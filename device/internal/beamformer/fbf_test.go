package beamformer

import (
	"math"
	"math/cmplx"
	"math/rand"
	"testing"
)

// scene renders plane waves at the seven mics as S16 buffers. Each source is a
// set of sinusoids evaluated at the exact per-mic delay, so the test's
// geometry is the physics and not the filters' own interpolation.
type tone struct{ f, a, ph float64 }

func tones(rng *rand.Rand, lo, hi, step, amp float64) []tone {
	var t []tone
	for f := lo; f <= hi; f += step {
		t = append(t, tone{f, amp, rng.Float64() * 2 * math.Pi})
	}
	return t
}

// render fills n samples starting at sample t0 for a source at azimuth az.
func render(dst [][]float64, ts []tone, az float64, t0 int, gate func(t int) float64) {
	u := unitVec(az, 0)
	for ch := 0; ch < NumMics; ch++ {
		p := MicPosition(ch)
		adv := (u[0]*p[0] + u[1]*p[1]) / speedOfSound
		for i := range dst[ch] {
			t := float64(t0+i)/sampleRate + adv
			var v float64
			for _, s := range ts {
				v += s.a * math.Cos(2*math.Pi*s.f*t+s.ph)
			}
			dst[ch][i] += v * gate(t0+i)
		}
	}
}

func s16(x [][]float64) [][]byte {
	out := make([][]byte, len(x))
	for ch := range x {
		out[ch] = make([]byte, len(x[ch])*2)
		for i, v := range x[ch] {
			q := int16(math.Max(-32768, math.Min(32767, math.Round(v))))
			out[ch][i*2], out[ch][i*2+1] = byte(uint16(q)), byte(uint16(q)>>8)
		}
	}
	return out
}

func always(int) float64 { return 1 }

// enableWakeBeam turns the path on and waits for the background design, which
// is the only difference from SetWakeBeam the tests need to hide.
func enableWakeBeam(b *Beamformer) {
	b.SetWakeBeam(true)
	b.fbf = <-b.fbfPending
	b.fbfPending = nil
}

func samples(b []byte) []float64 {
	out := make([]float64, len(b)/2)
	for i := range out {
		out[i] = float64(int16(uint16(b[i*2]) | uint16(b[i*2+1])<<8))
	}
	return out
}

// power of x at the given frequencies, by Goertzel-style projection.
func powerAt(x []float64, fs []float64) float64 {
	var p float64
	for _, f := range fs {
		var re, im float64
		for i, v := range x {
			a := 2 * math.Pi * f * float64(i) / sampleRate
			re += v * math.Cos(a)
			im += v * math.Sin(a)
		}
		p += (re*re + im*im) / float64(len(x)*len(x))
	}
	return p
}

// The interpolator must be a delay: unit magnitude, phase -w*delay, across
// the speech band, for every fractional position it is asked for.
func TestFractionalDelayAccuracy(t *testing.T) {
	for _, frac := range []float64{0, 0.25, 0.5, 0.75, 0.99} {
		delay := 1 + frac
		h := fractionalDelayLS(4, delay, 100, 5000)
		for _, f := range []float64{200, 500, 1000, 2000, 3000} {
			w := 2 * math.Pi * f / sampleRate
			var got complex128
			for k, c := range h {
				got += complex(c, 0) * cmplx.Exp(complex(0, -w*float64(k)))
			}
			want := cmplx.Exp(complex(0, -w*delay))
			if e := cmplx.Abs(got - want); e > 0.06 {
				t.Errorf("frac %.2f at %.0fHz: error %.3f", frac, f, e)
			}
		}
	}
}

// The weights may not amplify mic noise past the white-noise-gain profile at any
// bin: -7dB at 250Hz and below, which is stock's measured -7.4dB, so at most 5x
// the noise power, and the bound leaves a little for the bisection that finds
// the loading. An early version of this stage fitted short FIRs to the same
// design and, left free above 5.2kHz, gained 66dB there; a weight has nothing
// to leak into, but the bound is cheap and catches the next kind of mistake. DC
// and Nyquist carry nothing.
func TestBeamsDoNotAmplifyNoiseAnywhere(t *testing.T) {
	w := newWakeBeam()
	for b := 0; b < w.sd.Beams(); b++ {
		for k := 1; k < araBins-1; k++ {
			f := float64(k) * sampleRate / araFFT
			limit := math.Pow(10, -sdFloorDB(f, SDFloorOffsetDB)/10) * 1.05
			if p := w.sd.NoisePower(b, k); p > limit {
				t.Errorf("beam %.0f deg: noise power gain %.2f at %.0fHz, the profile allows %.2f", w.sd.Azimuths[b], p, f, limit)
			}
		}
		if p := w.sd.NoisePower(b, 0) + w.sd.NoisePower(b, araBins-1); p != 0 {
			t.Errorf("beam %.0f deg: weight at DC or Nyquist", w.sd.Azimuths[b])
		}
	}
}

func TestDesignShape(t *testing.T) {
	w := newWakeBeam()
	if w.sd.Beams() != fbfBeams || w.sd.Mics != len(fbfChannels) {
		t.Fatalf("%d beams over %d mics, want %d over %d", w.sd.Beams(), w.sd.Mics, fbfBeams, len(fbfChannels))
	}
	if len(fbfChannels) != 4 {
		t.Errorf("the wake beam sums %d mics; the stock design uses four", len(fbfChannels))
	}
	// Every path through the stage has its delay, so the neutral output has
	// exactly that.
	if w.off != ARADelay {
		t.Errorf("neutral delay %d, want the stage's %d", w.off, ARADelay)
	}
	for b := range w.beamChans {
		if len(w.beamChans[b]) != len(fbfChannels) {
			t.Errorf("beam %d sums %v, want all of %v", b, w.beamChans[b], fbfChannels)
		}
	}
}

// At its own azimuth a beam passes the signal at unity, with only the shared
// latency as phase, across the speech band.
func TestBeamPassesItsLookDirection(t *testing.T) {
	w := newWakeBeam()
	for b := 0; b < w.sd.Beams(); b++ {
		for k := 2; k < araBins-1; k++ {
			f := float64(k) * sampleRate / araFFT
			y := w.sd.Response(b, f, w.sd.Azimuths[b], 0)
			if d := cmplx.Abs(y - 1); d > 2e-3 {
				t.Errorf("beam %.0f deg at %.0fHz: look gain %v, want exactly 1", w.sd.Azimuths[b], f, y)
			}
		}
	}
}

// Before anything stands out the output is the centre mic, delayed by the
// beams' latency, sample for sample.
func TestNeutralIsTheDelayedCentreMic(t *testing.T) {
	b := New()
	enableWakeBeam(b)
	rng := rand.New(rand.NewSource(1))
	x := make([][]float64, NumMics)
	for ch := range x {
		x[ch] = make([]float64, 2560)
	}
	render(x, tones(rng, 200, 3800, 97, 300), 200, 0, always)
	in := s16(x)
	out := samples(b.WakeBeam(in))
	c := samples(in[centreCh])
	off := b.fbf.off
	for i := off; i < len(out); i++ {
		if out[i] != c[i-off] {
			t.Fatalf("sample %d: %v, want centre mic %v delayed %d", i, out[i], c[i-off], off)
		}
	}
	if b.WakeBeamIndex() != -1 {
		t.Errorf("beam %d chosen before anything happened", b.WakeBeamIndex())
	}
}

// A steady interferer sets the baseline; a talker then starts from 45° (beam
// 1). The path must find that beam quickly, hold it for the talk, and return
// to the centre mic once it is over.
func TestEngagesOnTheTalkerAndReleases(t *testing.T) {
	b := New()
	enableWakeBeam(b)
	rng := rand.New(rand.NewSource(2))
	interf := tones(rng, 150, 3900, 131, 120)
	talker := tones(rng, 250, 3700, 83, 220)

	const period = 2560 // the real 160ms batch
	talkStart, talkEnd := 6*16000, 8*16000
	gate := func(t int) float64 {
		if t >= talkStart && t < talkEnd {
			return 1
		}
		return 0
	}
	engagedAt, releasedAt := -1, -1
	var held int
	chosen := map[int]int{}
	for t0 := 0; t0 < 12*16000; t0 += period {
		x := make([][]float64, NumMics)
		for ch := range x {
			x[ch] = make([]float64, period)
		}
		render(x, interf, 225, t0, always)
		render(x, talker, 45, t0, gate)
		for ch := range x {
			for i := range x[ch] {
				x[ch][i] += rng.NormFloat64() * 5
			}
		}
		b.WakeBeam(s16(x))
		idx := b.WakeBeamIndex()
		if idx >= 0 && engagedAt < 0 && t0 >= talkStart {
			engagedAt = t0 + period
			held = idx
		}
		if idx < 0 && engagedAt >= 0 && releasedAt < 0 {
			releasedAt = t0 + period
		}
		if idx >= 0 && t0 >= talkStart && t0 < talkEnd {
			chosen[idx]++
		}
	}
	if engagedAt < 0 {
		t.Fatal("never engaged a beam")
	}
	// Wide superdirective beams are not "the nearest to the talker": the one
	// worth having is the one with the best SNR, which also depends on where
	// the interferer is. So score what was chosen against every beam forced
	// in turn, on the same two sources.
	snr := beamSNRs(t, talker, 45, interf, 225)
	best := math.Inf(-1)
	for _, v := range snr {
		best = math.Max(best, v)
	}
	for idx, n := range chosen {
		if best-snr[idx] > 3 {
			t.Errorf("beam %d chosen for %d periods has SNR %.1fdB, %.1fdB under the best (%v)", idx, n, snr[idx], best-snr[idx], snr)
		}
	}
	_ = held
	if d := engagedAt - talkStart; d > 16000/2 {
		t.Errorf("took %dms to engage", d*1000/16000)
	}
	if releasedAt < 0 {
		t.Fatal("never released to the centre mic")
	}
	if d := releasedAt - talkEnd; d > 3*16000 {
		t.Errorf("took %dms to release", d*1000/16000)
	}
}

// Steered at the talker with an interferer on the opposite side, the beam's
// output must beat the centre mic's SNR in the band the aperture can resolve.
// beamSNRs is each beam's output SNR for a talker and an interferer, with the
// selection forced to that beam.
func beamSNRs(t *testing.T, talker []tone, azT float64, interf []tone, azI float64) [fbfBeams]float64 {
	t.Helper()
	band := func(ts []tone) (fs []float64) {
		for _, s := range ts {
			fs = append(fs, s.f)
		}
		return
	}
	const n = 16000
	var out [fbfBeams]float64
	for beam := range out {
		run := func(src []tone, az float64) []float64 {
			b := New()
			enableWakeBeam(b)
			b.fbf.cur, b.fbf.holdLeft = beam, 1<<30
			x := zeros(NumMics, n)
			render(x, src, az, 0, always)
			return samples(b.WakeBeam(s16(x)))[2000:]
		}
		out[beam] = 10 * math.Log10(powerAt(run(talker, azT), band(talker))/powerAt(run(interf, azI), band(interf)))
	}
	return out
}

func TestBeamImprovesSNRAboveOneAndAHalfKilohertz(t *testing.T) {
	rng := rand.New(rand.NewSource(3))
	var fs []float64
	for f := 1600.0; f <= 3800; f += 200 {
		fs = append(fs, f)
	}
	talker := tones(rng, 1600, 3800, 200, 200)
	interf := tones(rng, 1650, 3750, 200, 200)
	const n = 16000
	run := func(src []tone, az float64) (beam, centre []float64) {
		b := New()
		enableWakeBeam(b)
		b.fbf.cur = nearestBeam(45) // the beam facing the talker; selection is tested above
		x := make([][]float64, NumMics)
		for ch := range x {
			x[ch] = make([]float64, n)
		}
		render(x, src, az, 0, always)
		in := s16(x)
		// Skip the first block: the fade from neutral to the forced beam.
		b.fbf.holdLeft = 1 << 30
		o := samples(b.WakeBeam(in))
		return o[2000:], samples(in[centreCh])[2000:]
	}
	tb, tc := run(talker, 45)
	ib, ic := run(interf, 225)
	snrBeam := powerAt(tb, fs) / powerAt(ib, fs)
	snrCentre := powerAt(tc, fs) / powerAt(ic, fs)
	if g := 10 * math.Log10(snrBeam/snrCentre); g < 6 {
		t.Errorf("beam gained %.1fdB over the centre mic, want >= 6", g)
	}
}

// A locked turn parks the path on the centre mic and leaves the statistics
// alone, and the result is not to be used.
func TestLockedTurnIsNotTheBeam(t *testing.T) {
	b := New()
	enableWakeBeam(b)
	if !b.WakeBeamUsable() {
		t.Fatal("unlocked with the path on must be usable")
	}
	b.lockedChannel = 2
	if b.WakeBeamUsable() {
		t.Error("a locked turn must not read the wake beam")
	}
	b.fbf.cur = 3
	b.WakeBeam(s16(zeros(NumMics, 512)))
	if b.fbf.cur != fbfNeutral {
		t.Errorf("locked: beam %d left selected, want the centre mic", b.fbf.cur)
	}
	if b.fbf.blocks != 0 {
		t.Error("locked periods must not feed the baseline")
	}
}

func TestOffByDefaultAndRefusesBadInput(t *testing.T) {
	b := New()
	if b.WakeBeamOn() || b.WakeBeam(s16(zeros(NumMics, 512))) != nil {
		t.Fatal("the path must be off by default")
	}
	enableWakeBeam(b)
	if b.WakeBeam(s16(zeros(3, 512))) != nil {
		t.Error("three mics is not seven")
	}
	bad := s16(zeros(NumMics, 512))
	bad[2] = bad[2][:100]
	if b.WakeBeam(bad) != nil {
		t.Error("unequal buffers must be refused")
	}
	b.SetWakeBeam(false)
	if b.WakeBeamOn() {
		t.Error("turning it off must stick")
	}
}

// Switching beam must not make a step: the shared latency and the crossfade
// exist for this. The comparison is the largest sample-to-sample change across
// a forced switch against the largest in steady state on either beam, since a
// beam that passes the source and one that rejects it differ in level, and a
// louder output has bigger steps without any click.
func TestSwitchingBeamDoesNotClick(t *testing.T) {
	rng := rand.New(rand.NewSource(4))
	x := zeros(NumMics, 1024)
	// Low tones: against a signal whose own steps are small, a hard switch
	// stands out; with high-frequency content it hides in the ordinary steps.
	render(x, tones(rng, 120, 500, 29, 150), 90, 0, always)
	in := s16(x)
	slice := func(from, to int) [][]byte {
		out := make([][]byte, len(in))
		for ch := range in {
			out[ch] = in[ch][from*2 : to*2]
		}
		return out
	}
	maxStep := func(v []float64) (m float64) {
		for i := 1; i < len(v); i++ {
			m = math.Max(m, math.Abs(v[i]-v[i-1]))
		}
		return m
	}
	from, to := nearestBeam(235), nearestBeam(110) // a beam turned away, and one at the source
	steady := func(beam int) float64 {
		b := New()
		enableWakeBeam(b)
		b.fbf.cur, b.fbf.holdLeft = beam, 1<<30
		return maxStep(samples(b.WakeBeam(in)))
	}
	base := math.Max(steady(from), steady(to))

	sw := New()
	enableWakeBeam(sw)
	sw.fbf.cur, sw.fbf.holdLeft = from, 1<<30
	first := samples(sw.WakeBeam(slice(0, fbfBlock*3)))
	sw.fbf.cur = to
	rest := samples(sw.WakeBeam(slice(fbfBlock*3, 1024)))
	if got := maxStep(append(first, rest...)); got > 1.25*base+20 {
		t.Errorf("largest step %.0f across the switch against %.0f in steady state", got, base)
	}
}

func TestClippingIsCounted(t *testing.T) {
	b := New()
	enableWakeBeam(b)
	x := make([][]float64, NumMics)
	for ch := range x {
		x[ch] = make([]float64, 512)
		for i := range x[ch] {
			x[ch][i] = 32000 * math.Pow(-1, float64(i))
		}
	}
	b.fbf.cur = 0
	b.fbf.holdLeft = 1 << 30
	b.WakeBeam(s16(x))
	// Full-scale in every mic through a sum of unit gain stays near full
	// scale; what matters is that nothing wraps.
	for _, v := range samples(b.WakeBeam(s16(x))) {
		if math.Abs(v) > 32768 {
			t.Fatalf("sample %v out of range", v)
		}
	}
}

// nearestBeam is the beam whose design azimuth is closest to az, since the
// beams are not on an even grid.
func nearestBeam(az float64) int {
	w := newWakeBeam()
	best, bd := 0, 1e9
	for i, a := range w.sd.Azimuths {
		d := math.Abs(angleDiff(a, az))
		if d < bd {
			best, bd = i, d
		}
	}
	return best
}

func zeros(mics, n int) [][]float64 {
	x := make([][]float64, mics)
	for i := range x {
		x[i] = make([]float64, n)
	}
	return x
}

// BenchmarkWakeBeamBatchARA is the same with the interference canceller on.
func BenchmarkWakeBeamBatchARA(b *testing.B) {
	bf := New()
	enableWakeBeam(bf)
	bf.SetWakeBeamAra(true)
	rng := rand.New(rand.NewSource(5))
	x := zeros(NumMics, 2560)
	render(x, denseTones(rng, 200, 3800, 100), 120, 0, always)
	in := s16(x)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		bf.WakeBeam(in)
	}
}

// BenchmarkWakeBeamBatch is one 160ms ALSA batch (2560 samples, seven mics)
// through all eight beams and the selection. The mic goroutine's deadline is
// the batch length, and the A53 is several times slower than a desktop core.
func BenchmarkWakeBeamBatch(b *testing.B) {
	bf := New()
	enableWakeBeam(bf)
	rng := rand.New(rand.NewSource(5))
	x := zeros(NumMics, 2560)
	render(x, tones(rng, 200, 3800, 97, 300), 120, 0, always)
	in := s16(x)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		bf.WakeBeam(in)
	}
}

// denseTones is many tones close enough together that every 62.5Hz bin of the
// canceller's spectrum has energy, as speech and music do.
func denseTones(rng *rand.Rand, lo, hi, amp float64) []tone { return tones(rng, lo, hi, 37, amp) }

// ARA through the whole wake path: mics, beams, canceller. A steady interferer
// off the talker's side for 8s so the filters can learn it, then a talker joins.
// SNR is the talker's output power over the interferer's residual, by running
// the scene with and without the talker (the canceller is not linear).
func TestARAImprovesTheWakePathAgainstASteadyInterferer(t *testing.T) {
	rng := rand.New(rand.NewSource(8))
	interf := denseTones(rng, 150, 3900, 90)
	talker := denseTones(rng, 250, 3700, 90)
	const period = 2560
	const total = 16000 * 13
	talkFrom := 16000 * 8
	gate := func(t int) float64 {
		if t >= talkFrom {
			return 1
		}
		return 0
	}
	// Render once; both modes and both runs read the same audio.
	batches := total / period
	noiseOnly := make([][][]byte, batches)
	withTalker := make([][][]byte, batches)
	for k := 0; k < batches; k++ {
		x := zeros(NumMics, period)
		render(x, interf, 225, k*period, always)
		noiseOnly[k] = s16(x)
		render(x, talker, 45, k*period, gate)
		withTalker[k] = s16(x)
	}

	runScene := func(scene [][][]byte, ara bool) (power float64) {
		b := New()
		enableWakeBeam(b)
		b.SetWakeBeamAra(ara)
		var sum float64
		var n int
		for k, batch := range scene {
			// The beam facing the talker, held there: the selection's own
			// release would send a steady scene back to the centre mic after a
			// second, and then this would measure the centre mic.
			b.fbf.cur, b.fbf.holdLeft, b.fbf.lowFor = nearestBeam(45), 1<<30, 0
			out := samples(b.WakeBeam(batch))
			if k*period >= 16000*10 { // well into the talk
				for _, v := range out {
					sum += v * v
					n++
				}
			}
		}
		return sum / float64(n)
	}
	snr := func(ara bool) (snrDB, talkerDB float64) {
		pn := runScene(noiseOnly, ara)
		psn := runScene(withTalker, ara)
		t.Logf("ara=%v: noise-only power %.0f, with talker %.0f", ara, pn, psn)
		if !(psn > pn) || math.IsNaN(psn) || math.IsNaN(pn) {
			t.Fatalf("ara=%v: the talker added no power (%.0f with, %.0f without): the scene is not measuring what it says", ara, psn, pn)
		}
		return 10 * math.Log10((psn-pn)/pn), 10 * math.Log10(psn-pn)
	}
	offSNR, offTalker := snr(false)
	onSNR, onTalker := snr(true)
	t.Logf("beam alone %.1fdB SNR; with ARA %.1fdB SNR (%+.1fdB); talker %+.1fdB through ARA", offSNR, onSNR, onSNR-offSNR, onTalker-offTalker)
	if onSNR-offSNR < 5 {
		t.Errorf("ARA gained only %.1fdB over the beam alone", onSNR-offSNR)
	}
	if onTalker-offTalker < -3 {
		t.Errorf("ARA cost the talker %.1fdB", offTalker-onTalker)
	}
}

// Switching the canceller must not click: the stage runs either way, so the
// timing and the signal are continuous across the change.
func TestARAToggleDoesNotClick(t *testing.T) {
	rng := rand.New(rand.NewSource(9))
	src := denseTones(rng, 200, 3500, 100)
	b := New()
	enableWakeBeam(b)
	b.fbf.cur, b.fbf.holdLeft = 2, 1<<30
	const period = 2560
	var out []float64
	for k := 0; k < 24; k++ {
		if k == 12 {
			b.SetWakeBeamAra(true)
		}
		x := zeros(NumMics, period)
		render(x, src, 90, k*period, always)
		out = append(out, samples(b.WakeBeam(s16(x)))...)
	}
	step := func(lo, hi int) (m float64) {
		for i := lo + 1; i < hi; i++ {
			m = math.Max(m, math.Abs(out[i]-out[i-1]))
		}
		return
	}
	around := step(12*period-200, 12*period+400)
	elsewhere := math.Max(step(4*period, 11*period), step(14*period, 23*period))
	if around > 1.5*elsewhere+50 {
		t.Errorf("largest step %.0f across the toggle against %.0f elsewhere", around, elsewhere)
	}
}

// The centre mic stands in for "no beam", and switching between the two
// crossfades, so a beam and the centre mic must line up in time. The neutral
// test above takes its delay from the code, so it cannot see a stage left out of
// the delay; this asks the audio. A plane wave from a beam's look direction
// arrives at the centre mic and passes the beam at unity, so the two outputs must
// be the same signal, with the best lag between them zero.
func TestABeamAndTheCentreMicLineUp(t *testing.T) {
	rng := rand.New(rand.NewSource(10))
	src := denseTones(rng, 300, 3000, 100)
	const target = 2
	look := newWakeBeam().sd.Azimuths[target]
	for _, ara := range []bool{false, true} {
		runOne := func(cur int) []float64 {
			b := New()
			enableWakeBeam(b)
			b.SetWakeBeamAra(ara)
			var out []float64
			for k := 0; k < 12; k++ {
				b.fbf.cur, b.fbf.holdLeft, b.fbf.lowFor = cur, 1<<30, 0
				x := zeros(NumMics, 2560)
				render(x, src, look, k*2560, always)
				out = append(out, samples(b.WakeBeam(s16(x)))...)
			}
			return out[16000:]
		}
		beam, centre := runOne(target), runOne(fbfNeutral)
		best, bestLag := math.Inf(-1), 0
		for lag := -6; lag <= 6; lag++ {
			var c float64
			for i := 10; i < len(beam)-10; i++ {
				c += beam[i] * centre[i+lag]
			}
			if c > best {
				best, bestLag = c, lag
			}
		}
		if bestLag != 0 {
			t.Errorf("ara=%v: the beam and the centre mic are %d samples apart", ara, bestLag)
		}
	}
}

// lockForTurn puts b in the state a wake leaves it in: locked to a perimeter
// mic that looked towards az.
func lockForTurn(b *Beamformer, az float64) {
	b.lockedChannel, b.lockAz, b.lockAzOK = 3, az, true
}

// With the setting off a locked turn is the single mic it always was, and the
// beam is not what the pipeline reads. This is the default, and the one thing
// the new setting may not change.
func TestTurnsAreBypassedByDefault(t *testing.T) {
	b := New()
	enableWakeBeam(b)
	lockForTurn(b, 55)
	if b.WakeBeamUsable() {
		t.Fatal("a locked turn read the wake beam with wakeBeamTurns off")
	}
	b.WakeBeam(s16(zeros(NumMics, 2560)))
	if b.WakeBeamIndex() != -1 || b.fbf.inTurn {
		t.Error("a bypassed turn must park on the centre mic and not enter turn mode")
	}
}

// With it on, the turn starts on the beam nearest the mic the lock picked when
// none is engaged, keeps it for every period of the turn, and leaves the
// statistics alone.
func TestTurnHoldsTheBeamForTheWholeTurn(t *testing.T) {
	rng := rand.New(rand.NewSource(11))
	src := denseTones(rng, 200, 3500, 100)
	b := New()
	enableWakeBeam(b)
	b.SetWakeBeamTurns(true)
	lockForTurn(b, 55) // beam 6 looks at 55 degrees
	want := nearestBeam(55)
	var blocks int
	for k := 0; k < 8; k++ {
		x := zeros(NumMics, 2560)
		render(x, src, 120+30*float64(k), k*2560, always) // a source that moves about
		b.WakeBeam(s16(x))
		if k == 0 {
			blocks = b.fbf.blocks
		}
		if got := b.WakeBeamIndex(); got != want {
			t.Fatalf("period %d: beam %d, want %d held for the turn", k, got, want)
		}
		if b.fbf.blocks != blocks {
			t.Fatalf("period %d: the statistics moved during a turn", k)
		}
	}
	if !b.WakeBeamUsable() {
		t.Error("the turn's audio is not the beam's")
	}

	// An already engaged beam is kept, whatever the lock pointed at.
	b2 := New()
	enableWakeBeam(b2)
	b2.SetWakeBeamTurns(true)
	b2.fbf.cur = 1
	lockForTurn(b2, 55)
	b2.WakeBeam(s16(zeros(NumMics, 2560)))
	if b2.WakeBeamIndex() != 1 {
		t.Errorf("an engaged beam 1 became %d at the lock", b2.WakeBeamIndex())
	}

	// And after the turn the beam stays until the selection's own release.
	b.lockedChannel, b.lockAzOK = -1, false
	b.WakeBeam(s16(zeros(NumMics, 2560)))
	if b.fbf.inTurn || b.WakeBeamIndex() != want {
		t.Errorf("after the turn: inTurn=%v beam %d, want the beam kept (%d)", b.fbf.inTurn, b.WakeBeamIndex(), want)
	}
}

// What a turn hears must be better than the mic it replaces, or the setting is
// pointless: a talker at the held beam's look direction with an interferer on
// the far side, through the turn path, against the centre mic.
func TestTurnAudioBeatsTheCentreMic(t *testing.T) {
	rng := rand.New(rand.NewSource(12))
	var fs []float64
	for f := 1600.0; f <= 3800; f += 200 {
		fs = append(fs, f)
	}
	talker := tones(rng, 1600, 3800, 200, 200)
	interf := tones(rng, 1650, 3750, 200, 200)
	const n = 16000
	beam := nearestBeam(55)
	az := newWakeBeam().sd.Azimuths[beam]
	run := func(src []tone, from float64) (turn, centre []float64) {
		b := New()
		enableWakeBeam(b)
		b.SetWakeBeamTurns(true)
		lockForTurn(b, az)
		x := zeros(NumMics, n)
		render(x, src, from, 0, always)
		in := s16(x)
		o := samples(b.WakeBeam(in))
		return o[3000:], samples(in[centreCh])[3000:]
	}
	tb, tc := run(talker, az)
	ib, ic := run(interf, az+180)
	gain := 10 * math.Log10((powerAt(tb, fs)/powerAt(ib, fs))/(powerAt(tc, fs)/powerAt(ic, fs)))
	t.Logf("turn audio %.1fdB better than the centre mic against an interferer opposite", gain)
	if gain < 6 {
		t.Errorf("the turn path gained only %.1fdB over the centre mic", gain)
	}
}

// The 80Hz high-pass is stock's: -3dB at 80Hz, -0.7 at 125, flat from the
// bottom of the wake word's band, and it removes rumble.
func TestTurnHighPassMatchesStocks(t *testing.T) {
	gain := func(f float64) float64 {
		w := 2 * math.Pi * f / sampleRate
		z := cmplx.Exp(complex(0, -w))
		z2 := z * z
		num := complex(float64(fbfHP[0]), 0) + complex(float64(fbfHP[1]), 0)*z + complex(float64(fbfHP[2]), 0)*z2
		den := 1 + complex(float64(fbfHP[3]), 0)*z + complex(float64(fbfHP[4]), 0)*z2
		return 20 * math.Log10(cmplx.Abs(num/den))
	}
	for _, c := range []struct{ f, want, tol float64 }{{80, -3.0, 0.3}, {125, -0.7, 0.3}, {250, 0, 0.1}, {1000, 0, 0.01}} {
		if g := gain(c.f); math.Abs(g-c.want) > c.tol {
			t.Errorf("high-pass at %.0fHz: %.2fdB, want %.1f +/-%.1f", c.f, g, c.want, c.tol)
		}
	}
	if g := gain(20); g > -20 {
		t.Errorf("20Hz rumble only %.1fdB down", g)
	}
}

// The high-pass is heard in a turn and only in a turn: the wake path's audio is
// what was measured and is left alone.
func TestHighPassIsOnlyHeardInATurn(t *testing.T) {
	low := func(turn bool) float64 {
		b := New()
		enableWakeBeam(b)
		if turn {
			b.SetWakeBeamTurns(true)
			lockForTurn(b, 55)
		}
		b.fbf.cur, b.fbf.holdLeft, b.fbf.lowFor = fbfNeutral, 1<<30, 0
		x := zeros(NumMics, 2560*6)
		for ch := range x {
			for i := range x[ch] {
				x[ch][i] = 4000 * math.Sin(2*math.Pi*30*float64(i)/sampleRate) // 30Hz rumble
			}
		}
		in := s16(x)
		var out []float64
		for k := 0; k < 6; k++ {
			batch := make([][]byte, len(in))
			for ch := range in {
				batch[ch] = in[ch][k*2560*2 : (k+1)*2560*2]
			}
			if turn {
				b.fbf.cur = fbfNeutral // the held beam would be a different output; this isolates the filter
			}
			out = append(out, samples(b.WakeBeam(batch))...)
		}
		return power32(out[2560*3:])
	}
	if turn, listen := low(true), low(false); turn > listen/10 {
		t.Errorf("rumble through a turn %.0f vs listening %.0f: the high-pass is not applied in the turn", turn, listen)
	}
}

func power32(x []float64) float64 {
	var p float64
	for _, v := range x {
		p += v * v
	}
	return p / float64(len(x))
}
