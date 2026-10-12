package main

import (
	"math"
	"math/cmplx"
	"math/rand"
	"testing"
)

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

// At the look direction the beam must be unity gain with the common delay
// removed, wherever the look direction is relative to the mics.
func TestLookDirectionUnity(t *testing.T) {
	pos := micPositions("all")
	for _, b := range Design(pos, 8, 4, 100, 5000) {
		for _, f := range []float64{250, 1000, 2500} {
			g := b.lookGain(pos, f)
			if m := 20 * math.Log10(cmplx.Abs(g)); math.Abs(m) > 0.3 {
				t.Errorf("beam %.0f° at %.0fHz: look gain %.2fdB", b.AzimuthDeg, f, m)
			}
			if p := cmplx.Phase(g); math.Abs(p) > 0.25 {
				t.Errorf("beam %.0f° at %.0fHz: look phase %.2frad", b.AzimuthDeg, f, p)
			}
		}
	}
}

// The beams are rotations of one another except for the mic layout, so with
// the hexagon's 60° symmetry beam 0 and a beam 180° round must have the same
// directivity.
func TestSymmetry(t *testing.T) {
	pos := micPositions("all")
	beams := Design(pos, 8, 4, 100, 5000)
	a := directivityIndex(pos, beams[0], 2000)
	b := directivityIndex(pos, beams[4], 2000)
	if math.Abs(a-b) > 0.3 {
		t.Errorf("DI at 2kHz differs between opposite beams: %.2f vs %.2f", a, b)
	}
}

// Plain delay-and-sum of M mics has a white-noise gain of 10log10(M) when
// the filters are ideal delays; four-tap filters must not lose more than a
// fraction of a dB, or the interpolators are attenuating.
func TestWhiteNoiseGain(t *testing.T) {
	pos := micPositions("all")
	b := Design(pos, 8, 4, 100, 5000)[0]
	want := 10 * math.Log10(float64(len(pos)))
	for _, f := range []float64{250, 1000, 2000} {
		if g := whiteNoiseGain(pos, b, f); math.Abs(g-want) > 0.5 {
			t.Errorf("WNG at %.0fHz = %.2f, want ~%.2f", f, g, want)
		}
	}
}

// The time-domain filter and the frequency-domain response must agree: a
// tone from the look direction comes out at the predicted amplitude.
func TestApplyMatchesResponse(t *testing.T) {
	pos := micPositions("all")
	b := Design(pos, 8, 4, 100, 5000)[0]
	const n = 1 << 13
	f := 1000.0 // an exact bin: 1000 = 512 * 16000/8192
	src := make([]complex128, n)
	k := int(f / sampleRate * n)
	src[k], src[n-k] = complex(n/2, 0), complex(n/2, 0)
	s := Source{src}
	y := b.Apply(s.AtMics(pos, 40, 0))
	// Mean square of a unit-amplitude cosine is 0.5.
	got := bandPower(y, 900, 1100) / 0.5
	r := b.Response(pos, f, 40, 0)
	want := real(r)*real(r) + imag(r)*imag(r)
	if math.Abs(got-want)/want > 0.02 {
		t.Errorf("time-domain power %.4f, frequency-domain %.4f", got, want)
	}
}

// A beam must beat a single mic against an interferer well off axis, in the
// band where the aperture can resolve it. It need not at low frequency; that
// is the finding the tool exists to quantify.
func TestBeamBeatsOmniAboveTwoKilohertz(t *testing.T) {
	pos := micPositions("all")
	b := Design(pos, 8, 4, 100, 5000)[0]
	rng := rand.New(rand.NewSource(3))
	const n = 1 << 14
	tgt := NewSource(rng, n, 1500, 4000, speechShape).AtMics(pos, 0, 0)
	intf := NewSource(rng, n, 1500, 4000, musicShape).AtMics(pos, 180, 0)
	centre := 6
	omni := bandPower(tgt[centre], 1500, 4000) / bandPower(intf[centre], 1500, 4000)
	beam := bandPower(b.Apply(tgt), 1500, 4000) / bandPower(b.Apply(intf), 1500, 4000)
	if db(beam/omni) < 6 {
		t.Errorf("beam gained only %.1fdB over one mic against a 180° interferer", db(beam/omni))
	}
}
