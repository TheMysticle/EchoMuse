package beamformer

import (
	"math"
	"math/rand"
	"testing"
)

// coloured is band-limited noise of the given rms: white noise through a short
// FIR, so adjacent samples correlate the way speech and music do.
func coloured(rng *rand.Rand, n int, rms float64) []float32 {
	raw := make([]float64, n+8)
	for i := range raw {
		raw[i] = rng.NormFloat64()
	}
	out := make([]float32, n)
	var p float64
	for i := range out {
		v := 0.5*raw[i+8] + 0.3*raw[i+7] + 0.15*raw[i+6] + 0.05*raw[i+5]
		out[i] = float32(v)
		p += v * v
	}
	g := float32(rms / math.Sqrt(p/float64(n)))
	for i := range out {
		out[i] *= g
	}
	return out
}

func power(x []float32) float64 {
	var p float64
	for _, v := range x {
		p += float64(v) * float64(v)
	}
	return p / float64(len(x))
}

// fir filters x by taps, with the first len(taps)-1 outputs using zeros.
func fir(x []float32, taps []float32) []float32 {
	y := make([]float32, len(x))
	for i := range y {
		for k, c := range taps {
			if i-k >= 0 {
				y[i] += c * x[i-k]
			}
		}
	}
	return y
}

func empty(n, beams int) [][]float32 {
	x := make([][]float32, beams)
	for b := range x {
		x[b] = make([]float32, n)
	}
	return x
}

// araRun feeds beams through an ARA in calls of `call` samples and returns the
// output stream per beam.
func araRun(a *ARA, in [][]float32, call int) [][]float32 {
	n := len(in[0])
	out := empty(n, len(in))
	for s := 0; s < n; s += call {
		e := min(s+call, n)
		chunk := make([][]float32, len(in))
		o := make([][]float32, len(in))
		for b := range in {
			chunk[b] = in[b][s:e]
			o[b] = out[b][s:e]
		}
		a.Process(chunk, o)
	}
	return out
}

// Whatever the call sizes, the stage reproduces its input delayed by ARADelay.
// It is the foundation: if this is wrong nothing downstream means anything.
// With cancelling on it must also be exact when there is nothing to cancel:
// one beam carries signal and its neighbours are silent, so no reference has
// anything to offer and nothing may be learnt.
func TestARAReconstructsItsInputDelayedByAFixedAmount(t *testing.T) {
	rng := rand.New(rand.NewSource(1))
	const n = 20000
	in := empty(n, 8)
	for b := range in {
		in[b] = coloured(rng, n, 3000)
	}
	one := empty(n, 8)
	one[0] = in[0]
	for _, call := range []int{2560, 1000, 512, 777, 128, 100} {
		for _, on := range []bool{false, true} {
			src := in
			if on {
				src = one
			}
			a := NewARA(8)
			a.SetEnabled(on)
			out := araRun(a, src, call)
			for b := 0; b < 8; b++ {
				var e, p float64
				for i := ARADelay + 400; i < n; i++ {
					d := float64(out[b][i]) - float64(src[b][i-ARADelay])
					e += d * d
					p += float64(src[b][i-ARADelay]) * float64(src[b][i-ARADelay])
				}
				if p > 0 && e/p > 1e-9 {
					t.Errorf("call %d on=%v beam %d: error %.2e of signal power", call, on, b, e/p)
				}
				// A silent beam shares a packed transform with a loud one and
				// picks up float32 rounding of it, about -140dB.
				if p == 0 {
					var loud float64
					for i := range src[0] {
						loud += float64(src[0][i]) * float64(src[0][i])
					}
					if e/loud > 1e-12 {
						t.Errorf("call %d on=%v beam %d: silence in, %.2e of the loud beam's power out", call, on, b, e/loud)
					}
				}
			}
		}
	}
}

// What cancelling costs when the beams are unrelated, as in a diffuse room:
// the filters chase noise and the output carries it. This is the misadjustment
// of the step size, and it bounds how small the steady step must be.
func TestARAAddsLittleNoiseToUnrelatedBeams(t *testing.T) {
	rng := rand.New(rand.NewSource(6))
	const n = 16000 * 10
	in := empty(n, 8)
	for b := range in {
		in[b] = coloured(rng, n, 3000)
	}
	a := NewARA(8)
	a.SetEnabled(true)
	out := araRun(a, in, 2560)
	var worst float64
	for b := 0; b < 8; b++ {
		var e, p float64
		for i := 16000 * 5; i < n; i++ {
			d := float64(out[b][i]) - float64(in[b][i-ARADelay])
			e += d * d
			p += float64(in[b][i-ARADelay]) * float64(in[b][i-ARADelay])
		}
		worst = math.Max(worst, e/p)
	}
	t.Logf("unrelated beams: output differs from input by %.1fdB (%.3f of the power)", 10*math.Log10(worst), worst)
	if worst > 0.06 {
		t.Errorf("cancelling added %.3f of the signal power as noise on unrelated beams", worst)
	}
}

// scene builds eight beams in which beam 0 hears the interferer through a
// short filter, beams 3 and 5 (its references) hear it directly, and the talker
// is in beam 0 and leaks into the references at leak (amplitude).
func scene(rng *rand.Rand, n int, talkFrom, talkTo int, interf, talk, leak float64) (in [][]float32, ntalk []float32) {
	in = empty(n, 8)
	n1, n2 := coloured(rng, n, interf), coloured(rng, n, interf)
	s := coloured(rng, n, talk)
	for i := range s {
		if i < talkFrom || i >= talkTo {
			s[i] = 0
		}
	}
	// What the target beam hears of the interferers: attenuated and filtered
	// differently to the references.
	h1 := fir(n1, []float32{0.0, 0.25, 0.1, 0.05})
	h2 := fir(n2, []float32{0.05, 0.15, 0.1})
	for i := 0; i < n; i++ {
		in[0][i] = s[i] + h1[i] + h2[i]
		in[3][i] = n1[i] + float32(leak)*s[i]
		in[5][i] = n2[i] + float32(leak)*s[i]
	}
	return in, s
}

// The point of the stage: learn the interferer from the references and take it
// out of the target beam, without touching what is not interference.
func TestARACancelsADirectionalInterferer(t *testing.T) {
	rng := rand.New(rand.NewSource(2))
	const n = 16000 * 12
	in, _ := scene(rng, n, 0, 0, 2000, 0, 0)
	a := NewARA(8)
	a.SetEnabled(true)
	out := araRun(a, in, 2560)

	lo, hi := 16000*9, n // after training
	before := power(in[0][lo:hi])
	after := power(out[0][lo:hi])
	cut := 10 * math.Log10(before/after)
	t.Logf("interferer cut %.1fdB (mu %.2f/%.2f, floor %.0fdB)", cut, ARAMu, ARAMuSteady, -20*math.Log10(ARAMinGain))
	if cut < 10 {
		t.Errorf("interferer cut %.1fdB, want >= 10 (floor is %.0fdB)", cut, -20*math.Log10(ARAMinGain))
	}
	if !a.Adapting(0) {
		t.Error("the filters were not adapting against a steady interferer")
	}
}

// The failure to design against: the talker speaks in the target beam and
// leaks into the references. The talker must come through, and the stage must
// have stopped learning while it spoke.
func TestARADoesNotCancelTheTalker(t *testing.T) {
	for _, leak := range []float64{0, 0.15, 0.3} {
		rng := rand.New(rand.NewSource(3))
		const n = 16000 * 14
		talkFrom, talkTo := 16000*9, 16000*13
		in, s := scene(rng, n, talkFrom, talkTo, 2000, 1500, leak)

		a := NewARA(8)
		a.SetEnabled(true)
		out := araRun(a, in, 2560)

		// The talker's contribution to the output: output minus what the
		// output was without the talker.
		in0, _ := scene(rand.New(rand.NewSource(3)), n, 0, 0, 2000, 0, leak)
		a0 := NewARA(8)
		a0.SetEnabled(true)
		out0 := araRun(a0, in0, 2560)

		lo, hi := talkFrom+16000, talkTo
		var num, den float64
		for i := lo; i < hi; i++ {
			d := float64(out[0][i]) - float64(out0[0][i])
			num += d * d
			den += float64(s[i-ARADelay]) * float64(s[i-ARADelay])
		}
		loss := 10 * math.Log10(den/num)
		t.Logf("leak %.2f: talker through the canceller %+.2fdB", leak, -loss)
		if loss > 2.5 {
			t.Errorf("leak %.2f: the talker lost %.1fdB through the canceller", leak, loss)
		}
	}
}

// With nothing but the talker, the stage should stay out of the way.
func TestARAStaysOutOfTheWayWhenThereIsNoInterferer(t *testing.T) {
	rng := rand.New(rand.NewSource(4))
	const n = 16000 * 8
	in, _ := scene(rng, n, 0, n, 0.001, 1500, 0.3)
	a := NewARA(8)
	a.SetEnabled(true)
	out := araRun(a, in, 2560)
	lo := 16000 * 2
	ratio := power(out[0][lo:]) / power(in[0][lo-ARADelay:n-ARADelay])
	if db := 10 * math.Log10(ratio); db < -1.5 {
		t.Errorf("a lone talker was cut by %.1fdB", -db)
	}
}

func BenchmarkARABatch(b *testing.B) {
	rng := rand.New(rand.NewSource(5))
	in := empty(2560, 8)
	for k := range in {
		in[k] = coloured(rng, 2560, 2000)
	}
	out := empty(2560, 8)
	a := NewARA(8)
	a.SetEnabled(true)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		a.Process(in, out)
	}
}

// Stock's gate, from libasp: full step while a beam's output is near its floor,
// half at an SNR of about 10, nearly nothing at 20. The curve is fitted to the
// firmware's 128 entries, so these are its measured values.
func TestStockGateCurve(t *testing.T) {
	for _, c := range []struct{ snr, want, tol float64 }{
		{1, 1.0, 0.01}, {3.4, 0.999, 0.01}, {5, 0.992, 0.01}, {7, 0.918, 0.01},
		{10, 0.575, 0.01}, {10.3, 0.517, 0.01}, {15, 0.141, 0.01}, {20, 0.048, 0.01}, {50, 0.005, 0.005},
	} {
		if g := araGate(c.snr); math.Abs(g-c.want) > c.tol {
			t.Errorf("gate(snr %.1f) = %.3f, stock's table gives %.3f", c.snr, g, c.want)
		}
	}
	// Monotonic: more SNR never opens it.
	prev := 2.0
	for snr := 0.0; snr < 100; snr += 0.25 {
		g := araGate(snr)
		if g > prev+1e-12 {
			t.Fatalf("gate rose at snr %.2f", snr)
		}
		prev = g
	}
}

// One beam over the half-way point stops every beam learning, not only itself:
// the talker is in one beam and leaks into the others' references.
func TestStockGateFreezesEveryBeamWhenOneIsAboveTheFloor(t *testing.T) {
	a := NewARA(8)
	a.SetGate(ARAGateStock)
	a.SetEnabled(true)
	for b := range a.gate {
		a.gate[b] = 1
	}
	a.gate[3] = 0.1 // beam 3's output SNR is high
	in := make([][]float32, 8)
	for b := range in {
		in[b] = make([]float32, araHop) // one hop: one frame, judged on the gates set above
		for i := range in[b] {
			in[b][i] = float32(100 * math.Sin(float64(i)*0.3+float64(b)))
		}
	}
	out := make([][]float32, 8)
	for b := range out {
		out[b] = make([]float32, araHop)
	}
	a.Process(in, out)
	if !a.freezeAll {
		t.Fatal("no freeze with one beam's gate at 0.1")
	}
	for b := 0; b < 8; b++ {
		if a.Adapting(b) {
			t.Errorf("beam %d learnt while another was over the line", b)
		}
	}
}
