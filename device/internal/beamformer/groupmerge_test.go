package beamformer

import (
	"math"
	"math/rand"
	"testing"
)

// The output of a group is the mean of its three beams, and the stock output
// gain is on top of it, and nothing else.
func TestGroupOutputIsTheMeanOfItsBeamsPlusStocksGain(t *testing.T) {
	rng := rand.New(rand.NewSource(5))
	src := denseTones(rng, 200, 3500, 100)
	b := New()
	enableWakeBeam(b)
	b.fbf.outGain = float32(math.Pow(10, fbfOutGainDB/20))
	g := 6 // beams 6, 7, 0 wrap the ring
	b.fbf.cur, b.fbf.sel, b.fbf.holdLeft = g, g, 1<<30
	x := zeros(NumMics, 2560)
	render(x, src, 40, 0, always)
	out := samples(b.WakeBeam(s16(x)))
	w := b.fbf
	for i := 100; i < len(out); i += 37 {
		want := (w.out[6][i] + w.out[7][i] + w.out[0][i]) / 3 * w.outGain
		if d := math.Abs(float64(softKnee(want)) - out[i]); d > 1 {
			t.Fatalf("sample %d: %v, want mean of beams 6,7,0 x %.2f = %v", i, out[i], w.outGain, want)
		}
	}
	if db := 20 * math.Log10(float64(w.outGain)); math.Abs(db-8.3) > 0.01 {
		t.Errorf("output gain %.2fdB, stock's is 8.3", db)
	}
}

// Group g is beams g, g+1, g+2, round the ring as stock's table lists them.
func TestGroupsAreStocksEightOverlappingTriples(t *testing.T) {
	want := [][3]int{{0, 1, 2}, {1, 2, 3}, {2, 3, 4}, {3, 4, 5}, {4, 5, 6}, {5, 6, 7}, {6, 7, 0}, {7, 0, 1}}
	for g, w := range want {
		if got := groupBeams(g); got != w {
			t.Errorf("group %d = %v, stock's is %v", g, got, w)
		}
	}
}

// The knee is the identity below it, bounded above, monotonic and smooth.
func TestSoftKnee(t *testing.T) {
	var prev float32 = -40000
	for v := float32(-40000); v <= 40000; v += 7 {
		y := softKnee(v)
		if math.Abs(float64(v)) <= fbfKnee && y != v {
			t.Fatalf("%v moved to %v under the knee", v, y)
		}
		if y > 32767 || y < -32767 {
			t.Fatalf("%v -> %v is past full scale", v, y)
		}
		if y < prev {
			t.Fatalf("%v -> %v is not monotonic (%v before)", v, y, prev)
		}
		prev = y
	}
	// Slope 1 at the knee (no corner).
	d := softKnee(fbfKnee+1) - softKnee(fbfKnee)
	if math.Abs(float64(d)-1) > 0.01 {
		t.Errorf("slope at the knee %v", d)
	}
}

// The noise floor is the average for the initial period and the minimum of the
// slow power after it, so a talker does not raise it and a quiet moment lowers
// it.
func TestNoiseFloorIsAMinimumAfterTheInitialPeriod(t *testing.T) {
	e := newBeamSNR()
	rng := rand.New(rand.NewSource(1))
	block := func(rms float64) []float32 {
		x := make([]float32, fbfBlock)
		for i := range x {
			x[i] = float32(rng.NormFloat64() * rms)
		}
		return x
	}
	n := 0
	run := func(rms float64, blocks int) float64 {
		var snr float64
		for i := 0; i < blocks; i++ {
			n++
			snr = e.add(block(rms), n)
		}
		return snr
	}
	run(200, fbfNoiseInitBlocks+fbfNoiseSlots*fbfNoiseSlotBlocks) // steady room
	room := e.floor
	// Speech for two seconds: the floor must not follow it up.
	snrSpeech := run(2000, 250)
	if e.floor > room*1.5 {
		t.Errorf("floor rose from %.0f to %.0f during speech", room, e.floor)
	}
	if snrSpeech < 20 {
		t.Errorf("speech 20dB over the room scored only %.1fx", snrSpeech)
	}
	// A quieter room lowers it, within the buffer's span (about 4s).
	run(50, fbfNoiseSlots*fbfNoiseSlotBlocks)
	if e.floor > room/4 {
		t.Errorf("floor %.0f did not follow the room down from %.0f", e.floor, room)
	}
}

// Steady room noise leaves every group scoring about the same, so the path may
// drift between them (harmless) but must not hop every second or two, as the
// onset selector did. What matters is never staying on a bad one, which the
// clack test below covers.
func TestSelectionIsCalmInSteadyNoise(t *testing.T) {
	b := New()
	enableWakeBeam(b)
	rng := rand.New(rand.NewSource(9))
	const period = 2560
	for t0 := 0; t0 < 40*16000; t0 += period {
		x := zeros(NumMics, period)
		for ch := range x {
			for i := range x[ch] {
				x[ch][i] = rng.NormFloat64() * 40
			}
		}
		b.WakeBeam(s16(x))
	}
	secs := 40 - float64(fbfWarmBlocks*fbfBlock)/16000
	if rate := float64(b.fbf.switches) / secs; rate > 0.5 {
		t.Errorf("%d group switches in %.0fs of steady noise: %.2f a second", b.fbf.switches, secs, rate)
	}
}

// A clack from the far side just before a word moves the path to it (it is the
// loudest thing in the room) and must not keep it there: the word's own group
// takes over inside half a second, however late it starts.
func TestAClackJustBeforeTheWordDoesNotHoldThePath(t *testing.T) {
	rng := rand.New(rand.NewSource(4))
	talker := tones(rng, 250, 3700, 83, 220)
	clack := denseTones(rng, 300, 3500, 400)
	interf := tones(rng, 150, 3900, 131, 60)
	const period = 2560
	wordAt := 10 * 16000
	snr := beamSNRs(t, talker, 45, interf, 225)
	best := math.Inf(-1)
	for _, v := range snr {
		best = math.Max(best, v)
	}

	b := New()
	enableWakeBeam(b)
	var idxs []int
	for t0 := 0; t0 < 12*16000; t0 += period {
		x := zeros(NumMics, period)
		render(x, interf, 225, t0, always)
		// The clack: 50ms, 150ms before the word, from the far side.
		render(x, clack, 225, t0, func(tt int) float64 {
			if tt >= wordAt-3200 && tt < wordAt-2400 {
				return 1
			}
			return 0
		})
		render(x, talker, 45, t0, func(tt int) float64 {
			if tt >= wordAt {
				return 1
			}
			return 0
		})
		for ch := range x {
			for i := range x[ch] {
				x[ch][i] += rng.NormFloat64() * 5
			}
		}
		b.WakeBeam(s16(x))
		idxs = append(idxs, b.WakeBeamIndex())
	}
	// Two periods (320ms) after the word starts, and for the rest of it.
	for k := wordAt/period + 3; k < len(idxs); k++ {
		if g := idxs[k]; g < 0 || best-snr[g] > 3 {
			t.Fatalf("%dms into the word the path is on group %d (SNRs %v)", (k*period-wordAt)*1000/16000, g, snr)
		}
	}
}
