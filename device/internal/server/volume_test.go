package server

import (
	"github.com/wilbowes/EchoMuse/pkg/led"
	"testing"
	"time"
)

// A deliberate button press must outrank the volume arc's 2s hold. Before
// this, adjusting volume then immediately pressing the action button left
// the arc owning the ring for the remainder of its window, so the device
// gave no sign it had started listening.
func TestCancelDisplayReleasesTheRing(t *testing.T) {
	vc := newVolumeController(func() led.Controller { return nil })

	vc.mu.Lock()
	vc.displayActive = true
	vc.timer = time.AfterFunc(volumeLEDSecs*time.Second, func() {})
	vc.mu.Unlock()

	if !vc.DisplayActive() {
		t.Fatal("precondition: arc should own the ring")
	}

	vc.CancelDisplay()

	if vc.DisplayActive() {
		t.Fatal("arc still owns the ring after CancelDisplay — a listening " +
			"frame would be recorded but not painted")
	}
	// Idempotent: a second press must not panic on the already-stopped timer.
	vc.CancelDisplay()
}

// tinymix ctl 61 spans 0..175, but 127 is the codec's 0dB. Above it the DAC
// applies positive digital gain to near-full-scale PCM and saturates —
// measured on hardware at 65% THD by index 153, 89% by 170, with the output
// level flat from 153 up because it had stopped getting louder. Stock FireOS
// never writes this control at all. If this constant creeps back toward 175,
// the garbling above ~73% volume returns.
func TestVolumeMaxIsCodecUnityNotTheControlMaximum(t *testing.T) {
	if volumeMax != 127 {
		t.Fatalf("volumeMax = %d, want 127 (0dB). Anything higher clips the DAC.",
			volumeMax)
	}
	if volumeButtonFloor >= volumeMax {
		t.Fatalf("button floor %d must sit below the ceiling %d",
			volumeButtonFloor, volumeMax)
	}
}

// The button band must be crossable in a sane number of presses: too few and
// each press is a huge jump, too many and reaching the top is a chore.
func TestButtonBandTakesAReasonableNumberOfPresses(t *testing.T) {
	presses := (volumeMax - volumeButtonFloor) / volumeStep
	if presses < 6 || presses > 16 {
		t.Fatalf("%d presses to cross the band (step %d over %d..%d); "+
			"want roughly 8-12", presses, volumeStep, volumeButtonFloor, volumeMax)
	}
}

func TestStepsStayInsideTheButtonBand(t *testing.T) {
	cases := []struct {
		name string
		in   int
		want int
	}{
		// A level below the floor — HA can set one, and so could a stored
		// level from before the cap — must reach audible in ONE press, not
		// creep up 4dB at a time through inaudible territory.
		{"far below the floor lands on it", volumeButtonFloor - 40, volumeButtonFloor},
		{"just below the floor lands on it", volumeButtonFloor - 1, volumeButtonFloor},
		{"inside the band is untouched", volumeButtonFloor + volumeStep, volumeButtonFloor + volumeStep},
		{"above the ceiling clamps down", volumeMax + 30, volumeMax},
	}
	for _, tc := range cases {
		if got := clampToButtonBand(tc.in); got != tc.want {
			t.Errorf("%s: clampToButtonBand(%d) = %d, want %d",
				tc.name, tc.in, got, tc.want)
		}
	}
}

// Stepping up from the top and down from the bottom must settle, not
// oscillate or run away past the band.
func TestSteppingSaturatesAtBothEnds(t *testing.T) {
	level := volumeMax
	for i := 0; i < 5; i++ {
		level = clampToButtonBand(level + volumeStep)
	}
	if level != volumeMax {
		t.Errorf("stepping up from the ceiling reached %d, want %d", level, volumeMax)
	}

	level = volumeButtonFloor
	for i := 0; i < 5; i++ {
		level = clampToButtonBand(level - volumeStep)
	}
	if level != volumeButtonFloor {
		t.Errorf("stepping down from the floor reached %d, want %d",
			level, volumeButtonFloor)
	}
}

func TestStepReportsWhetherTheLevelChanged(t *testing.T) {
	vc := newVolumeController(func() led.Controller { return nil })
	vc.Set(volumeMax, false, "test")
	if vc.StepUp() {
		t.Fatal("step up at the ceiling reported a change")
	}
	if !vc.StepDown() {
		t.Fatal("step down from the ceiling did not report a change")
	}
	vc.Set(volumeButtonFloor, false, "test")
	if vc.StepDown() {
		t.Fatal("step down at the floor reported a change")
	}
}

// radarVolumeSteps is stock's VolumeCurves.xml music row through stock's
// mixer level table — recomputed here from both, so a typo in either the
// ladder or the derivation fails. Pinned against controller/em_volume's
// STOCK_MIXER_LEVELS too (same table, value + 27 from 11 up).
func TestRadarVolumeStepsAreStocks(t *testing.T) {
	curve := []int{0, 1, 2, 3, 4, 6, 11, 16, 22, 28, 34, 40, 44, 46, 50, 54,
		56, 60, 64, 68, 70, 72, 76, 80, 84, 88, 90, 92, 96, 98, 100}
	low := []int{0, 3, 7, 11, 17, 20, 27, 30, 32, 35, 36}
	mixer := func(v int) int {
		if v <= 10 {
			return low[v]
		}
		return v + 27
	}
	if len(radarVolumeSteps) != 30 {
		t.Fatalf("%d steps, want 30 (stock's volume_step-01..30)", len(radarVolumeSteps))
	}
	for s := 1; s <= 30; s++ {
		if got, want := radarVolumeSteps[s-1], mixer(curve[s]); got != want {
			t.Errorf("step %d: level %d, want %d", s, got, want)
		}
	}
}

// On Radar the buttons walk stock's ladder: 29 presses from the bottom step
// reach unity, a level between steps lands on the next one, and neither end
// steps past itself.
func TestRadarButtonsWalkStocksSteps(t *testing.T) {
	vc := &volumeController{ledCtrl: func() led.Controller { return nil }, steps: radarVolumeSteps}
	vc.Set(0, false, "test")
	vc.StepUp()
	if vc.Get() != 3 {
		t.Fatalf("first press from silence: %d, want 3", vc.Get())
	}
	for i := 0; i < 29; i++ {
		vc.StepUp()
	}
	if vc.Get() != 127 {
		t.Fatalf("after 30 presses: %d, want 127", vc.Get())
	}
	vc.StepUp()
	if vc.Get() != 127 {
		t.Fatalf("past the top: %d", vc.Get())
	}
	vc.Set(80, false, "test") // between 77 and 81
	vc.StepUp()
	if vc.Get() != 81 {
		t.Errorf("up from 80: %d, want 81", vc.Get())
	}
	vc.Set(80, false, "test")
	vc.StepDown()
	if vc.Get() != 77 {
		t.Errorf("down from 80: %d, want 77", vc.Get())
	}
	vc.Set(3, false, "test")
	vc.StepDown()
	if vc.Get() != 3 {
		t.Errorf("down from the bottom step: %d, want 3", vc.Get())
	}
}

func TestStepIndex(t *testing.T) {
	for _, tc := range []struct{ level, idx int }{{0, 0}, {2, 0}, {3, 1}, {80, 14}, {81, 15}, {127, 30}} {
		if got := stepIndex(radarVolumeSteps, tc.level); got != tc.idx {
			t.Errorf("level %d: index %d, want %d", tc.level, got, tc.idx)
		}
	}
}

// The ring must agree with the number the user is looking at.
//
// The arc used to be drawn from the step index out of the board's 30-step
// ladder while every slider — Home Assistant's, the Spotify app's, the
// dashboard's — shows the 0-100 stock value. Two different scales, and it
// over-read worst at the bottom: measured on Radar, slider 3% lit 10% of the
// ring, 10% lit 17%, 20% lit 23%, 50% lit 47%.
func TestVolumeArcTracksTheSliderPercentage(t *testing.T) {
	const n = 12
	sum := func(a []int) (t int) {
		for _, v := range a {
			t += v
		}
		return
	}
	for _, pct := range []int{1, 3, 10, 20, 30, 50, 70, 90, 100} {
		got := sum(volumeArc(pct, n, false))
		want := (pct*2*n + 50) / 100
		if want < 1 {
			want = 1
		}
		if got != want {
			t.Errorf("%d%%: %d half-LEDs, want %d (%.0f%% of the ring)",
				pct, got, want, float64(got)/float64(2*n)*100)
		}
	}
	// Full means full, and nothing above silence is ever dark.
	if a := volumeArc(100, n, false); sum(a) != 2*n {
		t.Errorf("100%% = %v, want every LED full", a)
	}
	if a := volumeArc(0, n, false); sum(a) != 1 {
		t.Errorf("0%% = %v, want one half-bright LED rather than a dark ring", a)
	}
}

func TestVolumeArcGrowsClockwiseFromTheAnchor(t *testing.T) {
	const n = 12
	// The arc starts at the minus button and sweeps one way. It used to
	// start at index 0 — wherever index 0 happens to sit — and an earlier
	// attempt grew symmetrically outward, which is not what stock does.
	a := volumeArc(4, n, false)
	if a[ringVolumeStart] == 0 {
		t.Errorf("the anchor LED is dark at a low volume: %v", a)
	}
	for i, v := range a {
		if v != 0 && i != ringVolumeStart {
			t.Errorf("LED %d lit before the anchor filled: %v", i, a)
		}
	}
	// One direction from the anchor, wrapping, never symmetric.
	order := ringOrder(n, ringVolumeStart, ringVolumeSweep)
	for i := range order {
		if want := ((ringVolumeStart+i*ringVolumeSweep)%n + n) % n; order[i] != want {
			t.Fatalf("fill order %v does not sweep %+d from %d",
				order, ringVolumeSweep, ringVolumeStart)
		}
	}
	if len(order) != n {
		t.Fatalf("fill order covers %d LEDs, want %d", len(order), n)
	}
	seen := map[int]bool{}
	for _, i := range order {
		if seen[i] {
			t.Fatalf("LED %d appears twice in the fill order: %v", i, order)
		}
		seen[i] = true
	}
	// The arc is contiguous around the bottom at every level.
	for pct := 0; pct <= 100; pct++ {
		arc := volumeArc(pct, n, false)
		lit := 0
		for _, i := range order {
			if arc[i] == 0 {
				break
			}
			lit++
		}
		for _, i := range order[lit:] {
			if arc[i] != 0 {
				t.Fatalf("%d%%: LED %d lit beyond the arc: %v", pct, i, arc)
			}
		}
	}
}

// The bottom of the scale is a glyph straddling the anchor, not a fraction.
// It says "as quiet as it goes", which no fraction of a 12-LED ring can.
// Three LEDs was tried first and read as far too much.
func TestTheBottomOfScaleGlyph(t *testing.T) {
	const n = 12
	a := volumeArc(3, n, true)
	glyph := minGlyph(n, ringVolumeStart, minGlyphLEDs, ringVolumeSweep)
	if len(glyph) != minGlyphLEDs {
		t.Fatalf("glyph covers %d LEDs, want %d", len(glyph), minGlyphLEDs)
	}
	in := map[int]bool{}
	for _, i := range glyph {
		in[i] = true
		if a[i] != 2 {
			t.Errorf("glyph LED %d = %d, want full: %v", i, a[i], a)
		}
	}
	// It ends at the arc's start and runs counter-clockwise, so the arc
	// grows clockwise out of it rather than back over it.
	if glyph[0] != ringVolumeStart {
		t.Errorf("glyph starts at %d, want the anchor %d", glyph[0], ringVolumeStart)
	}
	for i, v := range a {
		if !in[i] && v != 0 {
			t.Errorf("LED %d lit in the minimum glyph: %v", i, a)
		}
	}
	// The glyph ignores the percentage: it is a state, not a level.
	if b := volumeArc(50, n, true); !equalInts(a, b) {
		t.Errorf("the glyph changed with the percentage: %v vs %v", a, b)
	}
}

func equalInts(a, b []int) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// The volume arc and the beamformer's direction arc must agree on where the
// bottom of the ring is, or one of them is pointing somewhere else.
func TestRingGeometryMatchesTheDirectionArc(t *testing.T) {
	// SetDirectionLEDs' own maths for 180 degrees, which is 6 o'clock.
	const nLEDs, ledOffset = numLEDs, ringLEDOffset
	primary := ((180 - ledOffset + 360) % 360) / 30 % nLEDs
	if primary != ringBottomLED {
		t.Errorf("direction arc puts 180 degrees at LED %d, the volume arc at %d",
			primary, ringBottomLED)
	}
}

// A Set that does not move the level must notify nobody.
//
// `changed` was computed and returned from the start and then ignored, so
// every Set told the controller, Sendspin and Spotify about a level that had
// not moved — and the Spotify side publishes it to Spotify's servers, which
// store volume as an integer percent and can hand it back. Re-announcing a
// level we already had invites it round again. Measured on a Radar
// 2026-10-11: 14,963 "Volume set to" lines in one session, values repeating
// in pairs, which is one real change notified twice.
func TestANoOpSetNotifiesNobody(t *testing.T) {
	vc := newVolumeController(func() led.Controller { return nil })
	var applied, notified int
	vc.SetApply(func(int) { applied++ })
	vc.SetOnVolumeChange(func(int) { notified++ })
	applied = 0 // SetApply applies once at wiring time, by design

	if !vc.Set(60, false, "test") {
		t.Fatal("the first Set to a new level reported no change")
	}
	if notified != 1 {
		t.Fatalf("one real change notified %d times, want 1", notified)
	}
	for i := 0; i < 20; i++ {
		if vc.Set(60, false, "test") {
			t.Fatal("an unchanged Set reported a change")
		}
	}
	if notified != 1 {
		t.Errorf("unchanged Sets notified %d times, want 1", notified)
	}
	// Still applied: idempotent, and the speaker starts silent until told a
	// level, so a Set matching vc.level may be the first the hardware sees.
	if applied != 21 {
		t.Errorf("apply ran %d times, want 21 (it is unconditional)", applied)
	}
}

func TestAChangeAfterNoOpsStillNotifies(t *testing.T) {
	// The gate must not latch: suppressing repeats cannot cost the next real
	// move, or the volume would stop tracking after any duplicate.
	vc := newVolumeController(func() led.Controller { return nil })
	var levels []int
	vc.SetOnVolumeChange(func(l int) { levels = append(levels, l) })
	vc.Set(50, false, "test")
	vc.Set(50, false, "test")
	vc.Set(70, false, "test")
	vc.Set(70, false, "test")
	vc.Set(50, false, "test")
	want := []int{50, 70, 50}
	if len(levels) != len(want) {
		t.Fatalf("notified %v, want %v", levels, want)
	}
	for i := range want {
		if levels[i] != want[i] {
			t.Fatalf("notified %v, want %v", levels, want)
		}
	}
}
