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

	"github.com/wilbowes/EchoMuse/internal/beamformer"
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
	beams := &designSet{pos: pos, das: Design(pos, 8, 4, 100, 5000)}
	a := directivityIndex(beams, 0, 2000)
	b := directivityIndex(beams, 4, 2000)
	if math.Abs(a-b) > 0.3 {
		t.Errorf("DI at 2kHz differs between opposite beams: %.2f vs %.2f", a, b)
	}
}

// Plain delay-and-sum of M mics has a white-noise gain of 10log10(M) when
// the filters are ideal delays; four-tap filters must not lose more than a
// fraction of a dB, or the interpolators are attenuating.
func TestWhiteNoiseGain(t *testing.T) {
	pos := micPositions("all")
	d := &designSet{pos: pos, das: Design(pos, 8, 4, 100, 5000)}
	want := 10 * math.Log10(float64(len(pos)))
	for _, f := range []float64{250, 1000, 2000} {
		if g := whiteNoiseGain(d, 0, f); math.Abs(g-want) > 0.5 {
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

// The tool's analytic numbers are only worth quoting if the beams, run through
// the firmware's own stage on a real signal, behave as they say. A tone at a bin
// centre must come out at the analytic gain, from the look direction and off
// axis. Between bin centres the weights of the neighbouring bins blend: the look
// direction must still be passed within half a dB, and an off-axis direction,
// where the response changes quickly from one bin to the next, within 4dB.
func TestSuperdirectiveBeamsInTheTimeDomainMatchTheirResponse(t *testing.T) {
	pos := micPositions("amazon4")
	sd := beamformer.DesignSuperdirective(pos, 8, 0)
	const n = 1 << 14
	const beam = 3
	for _, f := range []float64{312, 1000, 1031, 2000, 2093, 3500} {
		for _, az := range []float64{sd.Azimuths[beam], 40, 200} {
			src := make([]complex128, n)
			k := int(math.Round(f / sampleRate * n))
			src[k], src[n-k] = complex(n/2, 0), complex(n/2, 0)
			sig := scale(Source{src}.AtMics(pos, az, 0), 1000)
			out := formBeams(sd, sig, false)
			var got float64
			for i := n / 2; i < n-600; i++ {
				got += float64(out[beam][i]) * float64(out[beam][i])
			}
			got /= float64(n/2 - 600)
			r := sd.Response(beam, float64(k)*sampleRate/n, az, 0)
			want := 1000 * 1000 * 0.5 * (real(r)*real(r) + imag(r)*imag(r))
			dB := 10 * math.Log10(got/want)
			centre := math.Abs(f/62.5-math.Round(f/62.5)) < 0.01
			limit := 0.15 // dB
			switch {
			case !centre && az == sd.Azimuths[beam]:
				limit = 0.5
			case !centre:
				limit = 4
			}
			if math.Abs(dB) > limit {
				t.Errorf("%.0fHz from %.0f deg: output %.2fdB from the analytic gain, limit %.2f", f, az, dB, limit)
			}
		}
	}
}
