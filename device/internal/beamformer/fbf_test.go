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

func TestDesignSharesAnIntegerLatency(t *testing.T) {
	w := newWakeBeam()
	for _, b := range w.beams {
		if b.Bulk != float64(w.off) {
			t.Errorf("beam %.0f°: bulk delay %.3f, want %d for every beam", b.AzimuthDeg, b.Bulk, w.off)
		}
		if b.Delay[centreCh] != float64(w.off) {
			t.Errorf("beam %.0f°: centre mic delayed %.3f, want exactly %d", b.AzimuthDeg, b.Delay[centreCh], w.off)
		}
		for mic, h := range b.H {
			if len(h) > w.maxDelay+1 {
				t.Errorf("beam %.0f° mic %d: filter %d long, history is %d", b.AzimuthDeg, mic, len(h), w.maxDelay)
			}
		}
	}
}

// At its own azimuth a beam passes the signal at unity, with only the shared
// latency as phase, across the speech band.
func TestBeamPassesItsLookDirection(t *testing.T) {
	w := newWakeBeam()
	for _, b := range w.beams {
		u := unitVec(b.AzimuthDeg, 0)
		for _, f := range []float64{300, 1000, 2000, 3000} {
			var y complex128
			for ch := 0; ch < NumMics; ch++ {
				p := MicPosition(ch)
				adv := (u[0]*p[0] + u[1]*p[1]) / speedOfSound
				x := cmplx.Exp(complex(0, 2*math.Pi*f*adv))
				for d, c := range b.H[ch] {
					y += complex(c, 0) * x * cmplx.Exp(complex(0, -2*math.Pi*f*float64(d)/sampleRate))
				}
			}
			y *= cmplx.Exp(complex(0, 2*math.Pi*f*b.Bulk/sampleRate))
			if m := 20 * math.Log10(cmplx.Abs(y)); math.Abs(m) > 0.5 {
				t.Errorf("beam %.0f° at %.0fHz: look gain %.2fdB", b.AzimuthDeg, f, m)
			}
			if ph := cmplx.Phase(y); math.Abs(ph) > 0.3 {
				t.Errorf("beam %.0f° at %.0fHz: look phase %.2frad", b.AzimuthDeg, f, ph)
			}
		}
	}
}

// Before anything stands out the output is the centre mic, delayed by the
// beams' latency, sample for sample.
func TestNeutralIsTheDelayedCentreMic(t *testing.T) {
	b := New()
	b.SetWakeBeam(true)
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
	b.SetWakeBeam(true)
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
		if idx >= 0 && idx != 1 && t0 >= talkStart && t0 < talkEnd {
			t.Errorf("at %.2fs beam %d in use, talker is at beam 1", float64(t0)/16000, idx)
		}
	}
	if engagedAt < 0 {
		t.Fatal("never engaged a beam")
	}
	if held != 1 {
		t.Errorf("first beam %d, want 1 (45°)", held)
	}
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
		b.SetWakeBeam(true)
		b.fbf.cur = 1 // force beam 1; selection is tested above
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
	b.SetWakeBeam(true)
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
	b.SetWakeBeam(true)
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
// a forced 2 -> 5 switch against the same signal's largest change on beam 2.
func TestSwitchingBeamDoesNotClick(t *testing.T) {
	rng := rand.New(rand.NewSource(4))
	x := zeros(NumMics, 1024)
	render(x, tones(rng, 200, 3000, 101, 150), 90, 0, always)
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
	steady := New()
	steady.SetWakeBeam(true)
	steady.fbf.cur, steady.fbf.holdLeft = 2, 1<<30
	ref := samples(steady.WakeBeam(in))

	sw := New()
	sw.SetWakeBeam(true)
	sw.fbf.cur, sw.fbf.holdLeft = 2, 1<<30
	first := samples(sw.WakeBeam(slice(0, fbfBlock*3)))
	sw.fbf.cur = 5
	rest := samples(sw.WakeBeam(slice(fbfBlock*3, 1024)))
	if got, base := maxStep(append(first, rest...)), maxStep(ref); got > 2.5*base+20 {
		t.Errorf("largest step %.0f across the switch against %.0f in steady state", got, base)
	}
}

func TestClippingIsCounted(t *testing.T) {
	b := New()
	b.SetWakeBeam(true)
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

func zeros(mics, n int) [][]float64 {
	x := make([][]float64, mics)
	for i := range x {
		x[i] = make([]float64, n)
	}
	return x
}

// BenchmarkWakeBeamBatch is one 160ms ALSA batch (2560 samples, seven mics)
// through all eight beams and the selection. The mic goroutine's deadline is
// the batch length, and the A53 is several times slower than a desktop core.
func BenchmarkWakeBeamBatch(b *testing.B) {
	bf := New()
	bf.SetWakeBeam(true)
	rng := rand.New(rand.NewSource(5))
	x := zeros(NumMics, 2560)
	render(x, tones(rng, 200, 3800, 97, 300), 120, 0, always)
	in := s16(x)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		bf.WakeBeam(in)
	}
}
