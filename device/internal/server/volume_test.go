package server

import (
	"encoding/json"
	"math"
	"os"
	"testing"
	"time"

	"github.com/wilbowes/EchoMuse/pkg/led"
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

// The grid reaches unity in 1dB steps. (Upstream also pins one ring state
// per step through arcLEDs; this fork draws the ring from the slider's
// percentage instead, volumeArc, held by its own tests below.)
func TestTheGridReachesUnity(t *testing.T) {
	if volumeButtonFloor+volumeSteps*volumeStep != volumeMax {
		t.Fatalf("the grid from %d in steps of %d misses unity", volumeButtonFloor, volumeStep)
	}
	if volumeStep != 2 {
		t.Fatalf("step %d levels, want 2 (1dB)", volumeStep)
	}
}

func TestStepsStayInsideTheButtonBand(t *testing.T) {
	f, s := volumeButtonFloor, volumeStep
	cases := []struct {
		name    string
		in, dir int
		want    int
	}{
		// A level below the floor (HA can set one, and so can a stored level)
		// reaches audible in ONE press, whichever button.
		{"far below the floor, up", f - 40, +1, f},
		{"far below the floor, down", f - 40, -1, f},
		{"just below the floor, up", f - 1, +1, f},
		{"on the grid, up", f + 4*s, +1, f + 5*s},
		{"on the grid, down", f + 4*s, -1, f + 3*s},
		// Between steps (a level from before the grid) snaps to a neighbour.
		{"between steps, up", f + 4*s + 1, +1, f + 5*s},
		{"between steps, down", f + 4*s + 1, -1, f + 4*s},
		{"above the ceiling, up", volumeMax + 30, +1, volumeMax},
		{"above the ceiling, down", volumeMax + 30, -1, volumeMax},
	}
	for _, tc := range cases {
		if got := stepLevel(tc.in, tc.dir); got != tc.want {
			t.Errorf("%s: stepLevel(%d, %d) = %d, want %d",
				tc.name, tc.in, tc.dir, got, tc.want)
		}
	}
}

// Stepping up from the top and down from the bottom must settle, not
// oscillate or run away past the band; and floor to unity takes exactly
// volumeSteps presses.
func TestSteppingCrossesTheBandOneStateAtATime(t *testing.T) {
	level := volumeButtonFloor
	for i := 1; i <= volumeSteps; i++ {
		level = stepLevel(level, +1)
		if want := volumeButtonFloor + i*volumeStep; level != want {
			t.Fatalf("press %d: level %d, want %d", i, level, want)
		}
	}
	if level != volumeMax || stepLevel(level, +1) != volumeMax {
		t.Errorf("stepping up settled at %d, want %d", level, volumeMax)
	}
	for i := 0; i < volumeSteps+5; i++ {
		level = stepLevel(level, -1)
	}
	if level != volumeButtonFloor {
		t.Errorf("stepping down settled at %d, want %d", level, volumeButtonFloor)
	}
}

// Home Assistant (via the controller) and Sendspin give a percentage. Every
// one lands on a level some number of presses reaches, so the ring reads the
// same whichever way the volume got there; 0 stays silent and 1 is the floor.
func TestPercentLandsOnTheButtonGrid(t *testing.T) {
	if PercentToLevel(0) != 0 || PercentToLevel(1) != volumeButtonFloor || PercentToLevel(100) != volumeMax {
		t.Fatalf("endpoints: 0->%d 1->%d 100->%d", PercentToLevel(0), PercentToLevel(1), PercentToLevel(100))
	}
	prev := 0
	for pct := 1; pct <= 100; pct++ {
		level := PercentToLevel(float64(pct))
		if (level-volumeButtonFloor)%volumeStep != 0 {
			t.Errorf("%d%% -> level %d, off the grid", pct, level)
		}
		if level < prev {
			t.Errorf("%d%% -> %d, below %d for less", pct, level, prev)
		}
		prev = level
		// A level reported back lands on the same level again.
		if back := PercentToLevel(LevelToPercent(level)); back != level {
			t.Errorf("level %d -> %.2f%% -> %d", level, LevelToPercent(level), back)
		}
	}
	if LevelToPercent(0) != 0 || LevelToPercent(1) != 1 || LevelToPercent(volumeButtonFloor) != 1 {
		t.Error("silent must read 0 and anything audible at least 1")
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
		want := (pct*n*arcUnitsPerLED + 50) / 100
		if want < 1 {
			want = 1
		}
		if got != want {
			t.Errorf("%d%%: %d quanta, want %d (%.0f%% of the ring)",
				pct, got, want, float64(got)/float64(n*arcUnitsPerLED)*100)
		}
	}
	// Full means full, and nothing above silence is ever dark.
	if a := volumeArc(100, n, false); sum(a) != n*arcUnitsPerLED {
		t.Errorf("100%% = %v, want every LED full", a)
	}
	if a := volumeArc(0, n, false); sum(a) != 1 {
		t.Errorf("0%% = %v, want one quantum rather than a dark ring", a)
	}
	// Adjacent percents should differ when resolution allows (smooth steps).
	prev := sum(volumeArc(0, n, false))
	changed := 0
	for pct := 1; pct <= 100; pct++ {
		got := sum(volumeArc(pct, n, false))
		if got != prev {
			changed++
			prev = got
		}
	}
	if changed < 50 {
		t.Errorf("only %d distinct fill totals across 0–100%%; want finer steps", changed)
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

// The bottom of the scale is a fixed glyph on ringVolumeGlyphStart, not a
// fraction of the fill arc. Fill and glyph anchors are independent: the pulse
// was already correct on hardware while the fill needed a one-LED clockwise
// shift.
func TestTheBottomOfScaleGlyph(t *testing.T) {
	const n = 12
	a := volumeArc(3, n, true)
	glyph := minGlyph(n, ringVolumeGlyphStart, minGlyphLEDs, ringVolumeSweep)
	if len(glyph) != minGlyphLEDs {
		t.Fatalf("glyph covers %d LEDs, want %d", len(glyph), minGlyphLEDs)
	}
	in := map[int]bool{}
	for _, i := range glyph {
		in[i] = true
		if a[i] != arcUnitsPerLED {
			t.Errorf("glyph LED %d = %d, want full (%d): %v", i, a[i], arcUnitsPerLED, a)
		}
	}
	order := ringOrder(n, ringVolumeGlyphStart, ringVolumeSweep)
	for i, g := range glyph {
		if g != order[i] {
			t.Fatalf("glyph %v is not the glyph-order start %v", glyph, order[:len(glyph)])
		}
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

// A server's volume command must come back as the same number, or Music
// Assistant and the Echo correct each other forever.
func TestPercentEchoReturnsTheCommand(t *testing.T) {
	var e PercentEcho
	for pct := 0; pct <= 100; pct++ {
		if got := e.ToPercent(e.ToLevel(pct)); got != pct {
			t.Errorf("%d%% -> %d -> %d%%", pct, e.ToLevel(pct), got)
		}
	}
	// A level reached some other way (a button) reports its own percentage.
	e.ToLevel(50)
	if got := e.ToPercent(volumeMax); got != 100 {
		t.Errorf("unity reads %d%%", got)
	}
}

// The controller maps Home Assistant's volume with the same law
// (em_volume.ha_volume_to_device); controller/tests/test_volume.py writes
// testdata/volume_law.json from it, and this holds the Go to it.
func TestTheLawMatchesTheControllers(t *testing.T) {
	raw, err := os.ReadFile("testdata/volume_law.json")
	if err != nil {
		t.Fatal(err)
	}
	var v struct {
		PercentToLevel []int     `json:"percent_to_level"`
		LevelToPercent []float64 `json:"level_to_percent"`
	}
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	for pct, want := range v.PercentToLevel {
		if got := PercentToLevel(float64(pct)); got != want {
			t.Errorf("PercentToLevel(%d) = %d, controller says %d", pct, got, want)
		}
	}
	for level, want := range v.LevelToPercent {
		if got := LevelToPercent(level); math.Abs(got-want) > 1e-5 {
			t.Errorf("LevelToPercent(%d) = %f, controller says %f", level, got, want)
		}
	}
}
