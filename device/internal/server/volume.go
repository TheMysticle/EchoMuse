package server

import (
	"log"
	"math"
	"sync"
	"time"

	"github.com/wilbowes/EchoMuse/pkg/board"
	"github.com/wilbowes/EchoMuse/pkg/led"
)

const (
	volumeMin = 0

	// volumeMax is unity gain. The level keeps the DAC control's law (0.5dB
	// per step, 0dB at 127) though the volume is now applied in software
	// (speaker/swvolume.go), so levels mean what they always did. Above 127
	// the DAC applied positive gain to near-full-scale PCM and saturated —
	// measured 2026-08-13 at 65% THD by index 153, 89% by 170 — which is why
	// the scale stops here; device/CLAUDE.md, Volume, has the measurement.
	volumeMax = 127

	// volumeButtonFloor is the bottom of the band the PHYSICAL buttons
	// traverse. The scale is dB-linear, so index 0 is -63.5dB and roughly the
	// bottom third of the control is indistinguishable from silence; stepping
	// across it spends presses to go nowhere. Silencing the device is the
	// mute button's job, not the volume button's.
	//
	// Explicit Set() calls are deliberately NOT floored — HA's volume 0.0
	// has to still mean silent. A press from below the floor lands ON the
	// floor rather than adding a step, so one press always reaches audible.
	volumeButtonFloor = 47 // -40dB

	// volumeStep is 4dB per press: 10 presses to cross the button band.
	volumeStep    = 8
	volumeLEDSecs = 2 // how long to show volume ring

	// volumeBoot is the level before the controller seeds the stored one:
	// what Init used to leave the DAC at, and so what this read back.
	volumeBoot = 100
	numLEDs    = 12

	// ringLEDOffset places LED 0 on the ring: an angle in degrees clockwise
	// from 12 o'clock maps to ((angle - ringLEDOffset + 360) % 360) / 30.
	// SetDirectionLEDs has used this since the beamformer arc existed; the
	// volume arc now shares it rather than keeping a second copy.
	ringLEDOffset = 240

	// ringBottomLED is the LED at 6 o'clock by the direction arc's geometry:
	// ((180 - 240 + 360) % 360) / 30 = 10. A test pins it against
	// SetDirectionLEDs' own answer so the two cannot drift.
	//
	// NOTE this offset was calibrated for BISCUIT, against mic angles
	// confirmed by tone injection (internal/beamformer, micAngles). Whether
	// Radar's ring is mounted at the same rotation is unverified, which is
	// why the volume anchor below is its own constant rather than this one.
	ringBottomLED = ((180 - ringLEDOffset + 360) % 360) / 30

	// ringVolumeStart is the first LED of the volume *fill*, which then grows
	// CLOCKWISE (increasing index — LED i sits at ringLEDOffset + 30i
	// degrees clockwise from 12 o'clock).
	//
	// Hardware observation: the fill was one LED too far anticlockwise
	// relative to the physical ring, while the min-volume pulse glyph was
	// already correct. Keep the glyph on the geometric bottom (minus button)
	// and shift only the fill start one LED clockwise.
	ringVolumeStart = (ringBottomLED + 1) % numLEDs

	// ringVolumeGlyphStart is the anchor for the bottom-of-scale pulse glyph.
	// Independent of ringVolumeStart so the fill can be rotated without
	// moving the pulse that was already in the right place.
	ringVolumeGlyphStart = ringBottomLED

	// ringVolumeSweep is which way the arc grows from the anchor: +1 for
	// increasing LED index, -1 for decreasing.
	//
	// By the direction arc's geometry increasing index is clockwise (LED i
	// at ringLEDOffset + 30i degrees clockwise from 12 o'clock) — but that
	// offset was calibrated on biscuit, so on another board the ring may be
	// wound or mounted the other way and this is the knob for it. The glyph
	// always runs opposite, so the arc grows out of it rather than back over
	// it.
	ringVolumeSweep = 1

	// minGlyphLEDs is how wide the bottom-of-scale glyph is. The anchor falls
	// BETWEEN two LEDs when this is 2, which is what straddling the minus
	// button looks like; 1 puts a single LED under it. Three was tried first
	// and read as far too much.
	minGlyphLEDs = 2

	// arcUnitsPerLED is how many brightness steps each LED contributes to the
	// volume fill. Was effectively 2 (off / half / full), which only gave
	// 24 distinct ring states for 0–100% — many adjacent button and slider
	// levels looked identical. 10 steps per LED → 120 states, so almost
	// every percent (and every Radar button step) moves the ring. The
	// leading edge uses intermediate PWM values; full LEDs stay at full.
	arcUnitsPerLED = 10

	// minPulse* shape the bottom-of-scale breathe. A ~1.2s cycle reads as a
	// pulse rather than a flash, and the floor keeps the glyph visible at
	// its dimmest — going fully dark would read as the ring dropping out.
	minPulseTick   = 60 * time.Millisecond
	minPulsePeriod = 1200 * time.Millisecond
	minPulseFloor  = 0.35

	// minPulseExtra lengthens the DISPLAY WINDOW at the bottom of the scale,
	// so the glyph breathes a little longer than an ordinary arc shows for.
	// One pulse cycle is 1.2s against a 2s window, so without this the
	// glyph is cut off mid-breath and reads as a glitch.
	//
	// It extends the window rather than outliving it. The first version ran
	// the pulse on an unbounded ticker: the clear timer fired at 2s, handed
	// the ring back to whatever was underneath, and the pulse carried on
	// painting over it for ever.
	minPulseExtra = 1200 * time.Millisecond
)

// radarVolumeSteps are stock Radar's 30 button steps, as device levels: each
// Alexa step's value from VolumeCurves.xml (music row), through the level
// table stock's /system/bin/mixer turns a value into (Mixer_AlgoRampGain) —
// the same law as ours, 0.5dB per step with 127 = 0dB. Stock has an LED
// animation for each (volume_step-01 to -30). They replace the 4dB steps
// above the -40dB floor on Radar: 30 presses from -62dB to unity where those
// gave 10, so a press moves as far as it does on a stock Echo. Steps are
// finer in the middle and wider at the bottom, as stock's are. Controller
// side, em_volume reads the HA slider on the same table.
var radarVolumeSteps = []int{
	3, 7, 11, 17, 27, 38, 43, 49, 55, 61,
	67, 71, 73, 77, 81, 83, 87, 91, 95, 97,
	99, 103, 107, 111, 115, 117, 119, 123, 125, 127,
}

type volumeController struct {
	mu    sync.Mutex
	level int
	// pct turns a level into the 0-100 scale the sliders show; see
	// SetPercent. nil falls back to a proportion of the button band.
	pct func(level int) int
	// steps is the board's own button ladder (radarVolumeSteps); nil keeps
	// the volumeStep band above volumeButtonFloor.
	steps          []int
	ledCtrl        func() led.Controller // getter so we handle nil during boot
	timer          *time.Timer
	displayActive  bool        // volume arc currently on the ring — see DisplayActive
	isMuted        func() bool // set after construction to avoid circular dependency
	onVolumeChange func(int)   // set after construction; called after every Set()
	apply          func(int)   // applies a level to the audio; see SetApply
	// onDisplayExpire, when set, replaces the default clear-to-black at the
	// end of the display window: the server wires it to repaint the ring
	// from its stored controller state, so a volume press mid-turn hands
	// back to the listening/thinking/playing animation instead of going
	// dark. The muted → red-ring case stays here either way.
	onDisplayExpire func()
	// minPulse stops the bottom-of-scale pulse. Non-nil only while that
	// glyph is on the ring.
	minPulse chan struct{}
}

// DisplayActive reports whether the volume arc is currently on the ring.
// The server checks this to suppress controller LED paints (and the
// direction overlay) for the display window — without it, the turn
// animations repaint within one frame (~100ms) and the arc appears as a
// glitch rather than a reading.
func (vc *volumeController) DisplayActive() bool {
	vc.mu.Lock()
	defer vc.mu.Unlock()
	return vc.displayActive
}

// SetOnVolumeChange wires a callback invoked after every Set() call.
// B7 fix (2026-07-05 review): previously Server.SetVolumeChangeCallback
// reached directly into vc.mu/vc.onVolumeChange from outside this struct.
// Encapsulating the lock here keeps volumeController responsible for its
// own synchronisation, matching every other volumeController method.
func (vc *volumeController) SetOnVolumeChange(cb func(int)) {
	vc.mu.Lock()
	vc.onVolumeChange = cb
	vc.mu.Unlock()
}

func newVolumeController(ledGetter func() led.Controller) *volumeController {
	vc := &volumeController{
		ledCtrl: ledGetter,
		level:   volumeBoot,
	}
	if board.IDOf(board.Current()) == "radar" {
		vc.steps = radarVolumeSteps
	}
	log.Printf("Volume controller initialised at %d/%d", vc.level, volumeMax)
	return vc
}

// SetPercent wires how a level becomes the 0-100 number every slider shows
// (em_volume on the controller, StockVolumeValue on Radar, a proportion of
// volumeMax elsewhere). The ring is drawn from it, so the arc and the slider
// cannot disagree.
//
// Wired from cmd because the conversion is the BOARD's, and this package
// knows nothing about boards. Absent, the arc falls back to a proportion of
// the button band — the old behaviour.
func (vc *volumeController) SetPercent(fn func(level int) int) {
	vc.mu.Lock()
	vc.pct = fn
	vc.mu.Unlock()
}

// SetApply wires what actually changes the loudness — the speaker's software
// volume — and applies the current level at once, since the speaker starts
// silent until told.
func (vc *volumeController) SetApply(fn func(int)) {
	vc.mu.Lock()
	vc.apply = fn
	level := vc.level
	vc.mu.Unlock()
	if fn != nil {
		fn(level)
	}
}

// Set applies a new volume level (0–volumeMax). showRing paints the cyan
// volume arc for the 2s display window — physical button presses pass true;
// live remote sets pass the remoteVolumeArc setting; the boot-time SeedVolume
// always passes false.
//
// src names WHO asked, and it reaches the log line. The volume oscillated
// between two levels and the log said only that it had changed — "Volume set
// to 17/127" with no writer — so which of the five callers was driving it had
// to be guessed and tested against the hardware instead of read off the line.
// Anything with several writers and one shared message needs this.
func (vc *volumeController) Set(level int, showRing bool, src string) bool {
	level = clampVolumeLevel(level)

	vc.mu.Lock()
	changed := vc.level != level
	vc.level = level
	// Copy under the lock — SetOnVolumeChange writes this field under mu
	// from the main goroutine, and button events can fire before that
	// wiring completes (SubscribeToButton starts the evdev goroutines
	// first).
	cb := vc.onVolumeChange
	apply := vc.apply
	vc.mu.Unlock()

	// Applied unconditionally: it is idempotent, and the speaker starts
	// silent until told a level, so a Set that matches vc.level may still be
	// the first one the hardware has seen.
	if apply != nil {
		apply(level)
	}

	// The ring is unconditional too, and deliberately: an extra Volume Up at
	// the ceiling replays the max-level cue even though the level cannot
	// change, which is what makes the top of the range feel like a limit
	// rather than a dead button.
	if showRing {
		vc.showLEDs(level)
	}

	// NOTHING IS NOTIFIED ON A NO-OP. `changed` was computed and returned
	// here from the start, and ignored — so every Set told the controller,
	// Sendspin and Spotify about a level that had not moved.
	//
	// That is a pump, not just waste. The callback reaches
	// spotifyVolumeChanged, which hands the level to the engine, and
	// librespot's spirc then PUBLISHES it to Spotify's servers ("delayed
	// volume update for all devices"). Spotify stores volume as an integer
	// percent and can hand it back, so re-announcing a level we already had
	// invites it to come round again. Measured on a Radar 2026-10-11: 14,963
	// `Volume set to` lines in one session, the same values repeating in
	// pairs, which is this branch firing twice for one real change.
	//
	// It is the rule the engine's own mixer already follows for the same
	// reason — ReportingMixer::set_volume reports only on a change, because
	// spirc writes the volume on every state report.
	if changed {
		log.Printf("Volume set to %d/%d (%s)", level, volumeMax, src)
		if cb != nil {
			cb(level)
		}
	}
	return changed
}

func clampVolumeLevel(level int) int {
	if level < volumeMin {
		return volumeMin
	}
	if level > volumeMax {
		return volumeMax
	}
	return level
}

// CancelDisplay ends the volume arc's hold early, releasing the ring back to
// whatever wants to paint next.
//
// The hold exists to stop turn animations — which repaint every ~80ms — from
// stomping the arc within a frame of it appearing. It was never meant to
// outrank a deliberate press: adjusting the volume and immediately pressing
// the action button left the arc sitting there for the rest of its 2s with no
// sign the device had started listening.
//
// Deliberately does NOT repaint. The caller is about to start a turn, so its
// listening frame lands within a round trip; clearing to black here would put
// a visible dark gap between the two. The arc simply stops being sovereign.
func (vc *volumeController) CancelDisplay() {
	// The pulse paints on its own goroutine, so releasing the hold without
	// stopping it leaves it writing over whatever paints next.
	vc.stopMinPulse()
	vc.mu.Lock()
	if vc.timer != nil {
		vc.timer.Stop()
		vc.timer = nil
	}
	vc.displayActive = false
	vc.mu.Unlock()
}

// Get returns current volume level.
func (vc *volumeController) Get() int {
	vc.mu.Lock()
	defer vc.mu.Unlock()
	return vc.level
}

// StepUp increases volume by one step, within the button band.
func (vc *volumeController) StepUp() bool {
	vc.mu.Lock()
	level, steps := vc.level, vc.steps
	vc.mu.Unlock()
	if steps != nil {
		return vc.Set(stepAbove(steps, level), true, "button")
	}
	return vc.Set(clampToButtonBand(level+volumeStep), true, "button")
}

// StepDown decreases volume by one step, within the button band.
func (vc *volumeController) StepDown() bool {
	vc.mu.Lock()
	level, steps := vc.level, vc.steps
	vc.mu.Unlock()
	if steps != nil {
		return vc.Set(stepBelow(steps, level), true, "button")
	}
	return vc.Set(clampToButtonBand(level-volumeStep), true, "button")
}

// stepAbove is the first step above level, or the top step. A level between
// steps (HA can put it anywhere) moves to the next step up, so one press
// always lands on the ladder.
func stepAbove(steps []int, level int) int {
	for _, s := range steps {
		if s > level {
			return s
		}
	}
	return steps[len(steps)-1]
}

// stepBelow is the last step below level, or the bottom step: like the
// floor in clampToButtonBand, silencing is the mute button's job.
func stepBelow(steps []int, level int) int {
	for i := len(steps) - 1; i >= 0; i-- {
		if steps[i] < level {
			return steps[i]
		}
	}
	return steps[0]
}

// stepIndex is how many steps are at or below level: 1..len(steps) on the
// ladder, 0 below its bottom.
func stepIndex(steps []int, level int) int {
	n := 0
	for _, s := range steps {
		if s <= level {
			n++
		}
	}
	return n
}

// clampToButtonBand holds a stepped level inside [volumeButtonFloor,
// volumeMax]. A device sitting below the floor — HA can put it there, and so
// can a stored level from before the cap — lands ON the floor from one press
// instead of creeping up 4dB at a time through inaudible territory.
func clampToButtonBand(level int) int {
	if level < volumeButtonFloor {
		return volumeButtonFloor
	}
	if level > volumeMax {
		return volumeMax
	}
	return level
}

// ringOrder is the LED indices of the volume arc in fill order: from `start`,
// stepping by `sweep` around the ring.
//
// Stock sweeps one way from its anchor rather than growing symmetrically
// outward; symmetric was tried first and is wrong.
func ringOrder(n, start, sweep int) []int {
	out := make([]int, n)
	for i := range out {
		out[i] = ((start+i*sweep)%n + n) % n
	}
	return out
}

// minGlyph is the bottom-of-scale indicator: `width` LEDs starting at `start`
// and running with `sweep`.
//
// The fill arc and the pulse glyph use separate anchors (ringVolumeStart vs
// ringVolumeGlyphStart) because on hardware the correct pulse position and
// the correct fill start are one LED apart.
func minGlyph(n, start, width, sweep int) []int {
	order := ringOrder(n, start, sweep)
	return order[:min(width, len(order))]
}

// volumeArc is the ring for a volume, in arcUnitsPerLED quanta per LED
// (0 = off, arcUnitsPerLED = full). The leading edge takes a partial value
// so intermediate percentages and button steps are visible as brightness
// changes, not only as whole LEDs turning on.
//
// `pct` is the 0-100 scale EVERY SLIDER SHOWS — Home Assistant's, the
// Spotify app's, the dashboard's. The ring agrees with that number by
// construction.
//
// The arc grows from ringVolumeStart clockwise (ringVolumeSweep). A partial
// leading LED keeps the ring moving on nearly every press: with 10 units
// per LED there are 120 distinct states across 12 LEDs.
//
// `atMin` is the bottom of the scale, and it is a FIXED GLYPH rather than a
// proportion: full-bright LEDs on ringVolumeGlyphStart, as stock shows its
// lowest volume. It says "this is as quiet as it goes".
func volumeArc(pct, n int, atMin bool) []int {
	units := make([]int, n)
	order := ringOrder(n, ringVolumeStart, ringVolumeSweep)
	if atMin {
		// Glyph stays on ringVolumeGlyphStart (correct pulse position);
		// fill order above uses the shifted ringVolumeStart.
		for _, i := range minGlyph(n, ringVolumeGlyphStart, minGlyphLEDs, ringVolumeSweep) {
			units[i] = arcUnitsPerLED
		}
		return units
	}
	// Total quanta for this percentage, rounded. One quantum is the smallest
	// visible step on the leading LED.
	filled := (pct*n*arcUnitsPerLED + 50) / 100
	if filled < 1 {
		// Above silence but below one quantum still shows something: a ring
		// that reads as off when the device is merely quiet is a fault.
		filled = 1
	}
	if filled > n*arcUnitsPerLED {
		filled = n * arcUnitsPerLED
	}
	for _, i := range order {
		u := min(filled, arcUnitsPerLED)
		units[i] = u
		filled -= u
		if filled <= 0 {
			break
		}
	}
	return units
}

// startMinPulse breathes the bottom-of-scale glyph for the display window.
//
// Driven here rather than through the LED animator because the volume arc
// deliberately OWNS the ring against animations — DisplayActive suppresses
// them, so an animation would be suppressed by the very frame it is meant to
// be. It is one i2c write per tick for at most the display window, the same
// cost class the animator already pays at ~100ms.
func (vc *volumeController) startMinPulse(lc led.Controller, base []int) {
	vc.stopMinPulse()
	stop := make(chan struct{})
	vc.mu.Lock()
	vc.minPulse = stop
	vc.mu.Unlock()
	go func() {
		t := time.NewTicker(minPulseTick)
		defer t.Stop()
		step := 0
		for {
			select {
			case <-stop:
				return
			case <-t.C:
				step++
				// Phase from elapsed ticks, not a frame counter that drifts:
				// the same reason runPulse derives its phase from time.
				scale := minPulseFloor + (1-minPulseFloor)*
					(1+math.Cos(2*math.Pi*float64(step)*float64(minPulseTick)/
						float64(minPulsePeriod)))/2
				leds := make([]led.Led, numLEDs)
				for i := 0; i < numLEDs; i++ {
					full := (200*base[i] + arcUnitsPerLED/2) / arcUnitsPerLED
					v := uint8(float64(full) * scale)
					leds[i] = led.Led{ID: i, R: 0, G: v, B: v}
				}
				if lc.SetLEDs(leds...) != nil {
					return
				}
			}
		}
	}()
}

// stopMinPulse ends the pulse if one is running. Idempotent.
func (vc *volumeController) stopMinPulse() {
	vc.mu.Lock()
	stop := vc.minPulse
	vc.minPulse = nil
	vc.mu.Unlock()
	if stop != nil {
		close(stop)
	}
}

// showLEDs lights N of 12 LEDs in cyan proportional to volume, then clears after 2s.
func (vc *volumeController) showLEDs(level int) {
	lc := vc.ledCtrl()
	if lc == nil {
		return
	}

	vc.mu.Lock()
	pctOf, steps := vc.pct, vc.steps
	vc.mu.Unlock()

	// The arc reads the 0-100 scale the sliders show, so the ring agrees
	// with whatever number the user is looking at. Without a conversion
	// wired, fall back to a proportion of the BUTTON band: over 0..volumeMax
	// the audible range crowds into the top LEDs.
	pct := 0
	if pctOf != nil {
		pct = pctOf(level)
	} else if span := volumeMax - volumeButtonFloor; span > 0 {
		pct = (level - volumeButtonFloor) * 100 / span
	}
	pct = min(max(pct, 0), 100)

	// The bottom of the scale is a glyph, not a fraction — the lowest step
	// of the board's ladder, or the button floor without one.
	atMin := level > volumeMin && ((steps != nil && level <= steps[0]) ||
		(steps == nil && level <= volumeButtonFloor))
	units := volumeArc(pct, numLEDs, atMin)

	leds := make([]led.Led, numLEDs)
	for i := 0; i < numLEDs; i++ {
		// Map 0..arcUnitsPerLED → 0..200 cyan (same full brightness as before).
		b := uint8((200*units[i] + arcUnitsPerLED/2) / arcUnitsPerLED)
		leds[i] = led.Led{ID: i, R: 0, G: b, B: b}
	}
	if err := lc.SetLEDs(leds...); err != nil {
		log.Printf("Volume LED set failed: %v", err)
		return
	}
	// At the bottom of the scale the glyph PULSES for the display window,
	// as stock's does. It is what distinguishes "as quiet as it goes" from a
	// small arc, which a static three LEDs cannot.
	if atMin {
		vc.startMinPulse(lc, units)
	} else {
		vc.stopMinPulse()
	}
	hold := volumeLEDSecs * time.Second
	if atMin {
		hold += minPulseExtra
	}

	// Cancel any existing clear timer and start a new one
	vc.mu.Lock()
	vc.displayActive = true
	if vc.timer != nil {
		vc.timer.Stop()
	}
	vc.timer = time.AfterFunc(hold, func() {
		vc.stopMinPulse()
		vc.mu.Lock()
		vc.displayActive = false
		expire := vc.onDisplayExpire
		vc.mu.Unlock()
		if vc.isMuted != nil && vc.isMuted() {
			// Restore mute indicator — red ring
			leds := make([]led.Led, numLEDs)
			for i := 0; i < numLEDs; i++ {
				leds[i] = led.Led{ID: i, R: 180, G: 0, B: 0}
			}
			lc.SetLEDs(leds...)
		} else if expire != nil {
			// Hand back to whatever the controller last painted —
			// listening/thinking/playing ring mid-turn, all-off when idle.
			expire()
		} else {
			clearLeds(lc)
		}
	})
	vc.mu.Unlock()
}
