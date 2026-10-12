package main

import (
	"fmt"
	"math"
	"math/cmplx"
	"math/rand"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// At the look direction the beam must be unity gain with the common delay
// removed, wherever the look direction is relative to the mics.
func TestLookDirectionUnity(t *testing.T) {
	pos := micPositions("all")
	for _, b := range Design(pos, 8, 4, 100, 5000) {
		for _, f := range []float64{250, 1000, 2500} {
			g := lookGain(b, pos, f)
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
	y := apply(b, s.AtMics(pos, 40, 0))
	// Mean square of a unit-amplitude cosine is 0.5.
	got := bandPower(y, 900, 1100) / 0.5
	r := response(b, pos, f, 40, 0)
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
	beam := bandPower(apply(b, tgt), 1500, 4000) / bandPower(apply(b, intf), 1500, 4000)
	if db(beam/omni) < 6 {
		t.Errorf("beam gained only %.1fdB over one mic against a 180° interferer", db(beam/omni))
	}
}

// The table loader must refuse anything that is not the 8-beam, 4-mic shape,
// since a different size read with this layout would give plausible nonsense.
func TestAmazonLoaderChecksTheShape(t *testing.T) {
	dir := t.TempDir()
	write := func(name string, n int) string {
		var b strings.Builder
		b.WriteString("/* synthetic */\n")
		for i := 0; i < n; i++ {
			fmt.Fprintf(&b, "%.12f,\n", float64(i%7)/10)
		}
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, []byte(b.String()), 0o644); err != nil {
			t.Fatal(err)
		}
		return p
	}
	if _, err := loadAmazon(write("short.cfg", 100)); err == nil {
		t.Error("a 100-value table was accepted")
	}
	tab, err := loadAmazon(write("ok.cfg", amzBeams*amzMics*amzBands*amzTaps*2))
	if err != nil {
		t.Fatal(err)
	}
	// Beam, mic, band, tap are slowest to fastest, then re, im.
	i := (((3*amzMics+2)*amzBands + 5) * amzTaps * 2)
	want := complex(float64(i%7)/10, float64((i+1)%7)/10)
	if got := tab.w(3, 2, 5, 0); got != want {
		t.Errorf("w(3,2,5,0) = %v, want %v", got, want)
	}
}
