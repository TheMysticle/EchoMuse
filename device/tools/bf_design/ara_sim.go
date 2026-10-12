package main

// Does the interference canceller (internal/beamformer/ara.go) improve what the
// wake word hears, beyond what the beams already give?
//
// The scene is the one it exists for: a steady interferer off to one side for
// 9s, so the filters can learn it, then a talker joins. The canceller adapts,
// so signal and noise cannot be separated by linearity as the other tables do;
// each scene is run twice, noise only and noise plus talker, and the talker's
// contribution is the difference, which is exact only while the filters are
// frozen during the talk. They are meant to be, so a large gap between this
// number and the target-preservation column is itself a finding.

import (
	"flag"
	"fmt"
	"math"
	"math/cmplx"
	"math/rand"
	"strconv"
	"strings"

	"github.com/wilbowes/EchoMuse/internal/beamformer"
)

// reverb makes the interferer arrive by five paths, as in a room: the direct
// sound and four reflections from other directions, later and weaker (delay in
// ms, gain in dB, azimuth offset from the direct path). Each is the same
// source delayed, so the arrivals are coherent, as reflections are.
var (
	reverb  bool
	offsets string
)

var reflections = []struct{ ms, gainDB, azOff float64 }{
	{2.3, -4, 70}, {5.7, -6, -110}, {11, -8, 170}, {21, -10, -40},
}

func araFlags() {
	flag.StringVar(&offsets, "ara.offsets", "90,180", "interferer azimuths relative to the talker, degrees, comma separated")
	flag.BoolVar(&reverb, "reverb", false, "the interferer arrives by a direct path and four reflections")
	flagFloat(&beamformer.ARAMu, "ara.mu", "ARA step size while young")
	flagFloat(&beamformer.ARAMuSteady, "ara.steady", "ARA steady step size")
	flagFloat(&beamformer.ARARatio, "ara.ratio", "adapt while references exceed this multiple of the target beam")
	flagFloat(&beamformer.ARALeak, "ara.leak", "ARA weight leakage per adapting frame")
	flagFloat(&beamformer.ARAMinGain, "ara.floor", "ARA per-bin suppression floor (amplitude)")
}

// araRunStream runs one mic scene through the beams and the canceller and
// returns each beam's output power over [lo, hi). For the superdirective beams
// that is the firmware's own stage (beams formed from the mics' spectra, then
// cancelling); for the delay-and-sum baseline the beams are filtered in the
// time domain and handed to the same canceller.
func araRunStream(d *designSet, sig [][]float64, on bool, lo, hi int) []float64 {
	n := len(sig[0])
	var out [][]float32
	if d.sd != nil {
		out = formBeams(d.sd, sig, on)
	} else {
		raw := make([][]float32, d.n())
		for b := range raw {
			y := apply(d.das[b], sig)
			raw[b] = make([]float32, n)
			for i, v := range y {
				raw[b][i] = float32(v)
			}
		}
		a := beamformer.NewARA(d.n())
		a.SetEnabled(on)
		out = make([][]float32, d.n())
		for b := range out {
			out[b] = make([]float32, n)
		}
		const call = 2560
		for s := 0; s < n; s += call {
			e := min(s+call, n)
			in := make([][]float32, d.n())
			o := make([][]float32, d.n())
			for b := range in {
				in[b], o[b] = raw[b][s:e], out[b][s:e]
			}
			a.Process(in, o)
		}
	}
	p := make([]float64, d.n())
	for b := range p {
		for i := lo; i < hi; i++ {
			p[b] += float64(out[b][i]) * float64(out[b][i])
		}
		p[b] /= float64(hi - lo)
	}
	return p
}

func araReport(rng *rand.Rand, pos [][2]float64, beams *designSet, inrDB, dnrDB, selfDB float64) {
	const (
		n         = 1 << 18
		lo, hi    = 100.0, 4000.0
		level     = 1000.0 // rms at a mic, int16 units: the canceller's thresholds are in them
		talkStart = 9 * 16000
		winLo     = 10*16000 + 8000
	)
	winHi := n - 2*beamformer.ARADelay
	gate := func(x [][]float64) [][]float64 {
		o := make([][]float64, len(x))
		for i := range x {
			o[i] = make([]float64, len(x[i]))
			copy(o[i][talkStart:], x[i][talkStart:])
		}
		return o
	}
	diffuse := scale(Diffuse(rng, pos, n, 32, lo, hi), level*math.Pow(10, dnrDB/20))
	self := scale(SelfNoise(rng, len(pos), n, lo, hi), level*math.Pow(10, selfDB/20))
	interfAmp := level * math.Pow(10, inrDB/20)

	type acc struct{ single, plain, ara, targetPlain, targetAra, cut float64 }
	res := map[float64]*acc{}
	count := map[float64]int{}
	var offs []float64
	for _, f := range strings.Split(offsets, ",") {
		if v, err := strconv.ParseFloat(strings.TrimSpace(f), 64); err == nil {
			offs = append(offs, v)
		}
	}
	for _, off := range offs {
		res[off] = &acc{}
		for k := 0; k < 8; k++ {
			az := 22.5 + 45*float64(k) // between beams: the hard case
			tgt := gate(scale(NewSource(rng, n, lo, hi, speechShape).AtMics(pos, az, 0), level))
			isrc := NewSource(rng, n, lo, hi, musicShape)
			intf := scale(isrc.AtMics(pos, az+off, 0), interfAmp)
			if reverb {
				for _, r := range reflections {
					d := delayed(isrc, r.ms/1000)
					refl := scale(d.AtMics(pos, az+off+r.azOff, 0), interfAmp*math.Pow(10, r.gainDB/20))
					intf = sumSignals(intf, refl)
				}
			}
			noise := sumSignals(intf, diffuse, self)
			both := sumSignals(tgt, noise)

			// The beam with the most target gain at this azimuth.
			best, bp := 0, -1.0
			for b := 0; b < beams.n(); b++ {
				var s float64
				for _, f := range []float64{500, 1000, 2000, 3000} {
					r := beams.resp(b, f, az, 0)
					s += real(r)*real(r) + imag(r)*imag(r)
				}
				if s > bp {
					best, bp = b, s
				}
			}
			// The mic nearest the talker, for the single-mic reference.
			near, nd := 0, math.Inf(1)
			for i, p := range pos {
				if d := angDiff(az, math.Atan2(p[0], p[1])*180/math.Pi); d < nd {
					near, nd = i, d
				}
			}
			mic := func(sig [][]float64) float64 {
				var p float64
				for i := winLo; i < winHi; i++ {
					p += sig[near][i] * sig[near][i]
				}
				return p / float64(winHi-winLo)
			}
			pn, psn := mic(noise), mic(both)
			a := res[off]
			a.single += db((psn - pn) / pn)

			for _, on := range []bool{false, true} {
				n0 := araRunStream(beams, noise, on, winLo, winHi)[best]
				s0 := araRunStream(beams, both, on, winLo, winHi)[best]
				snr := db((s0 - n0) / n0)
				if on {
					a.ara += snr
					a.targetAra += db(s0 - n0)
				} else {
					a.plain += snr
					a.targetPlain += db(s0 - n0)
				}
			}
			a.cut += 0
			count[off]++
		}
	}
	fmt.Printf("\nbeams with and without the interference canceller; interferer %+.0fdB, diffuse %+.0fdB, self-noise %+.0fdB re target;\n", inrDB, dnrDB, selfDB)
	fmt.Printf("talker joins after 9s, SNR over 10.5-16s, 8 target azimuths between beams, best beam each, wideband 100-4000Hz\n")
	fmt.Printf("%-14s %12s %16s %20s %14s\n", "interferer", "single mic", "beams, no ARA", "beams + ARA", "ARA gain")
	for _, off := range offs {
		a, c := res[off], float64(count[off])
		fmt.Printf("+%.0f deg %17.2f %14.2f dB %17.2f dB %+11.2f dB\n", off, a.single/c, a.plain/c, a.ara/c, (a.ara-a.plain)/c)
	}
	fmt.Printf("\ntalker level through the beam, ARA relative to no ARA (0 = untouched):")
	for _, off := range offs {
		a, c := res[off], float64(count[off])
		fmt.Printf("  +%.0f deg %+.2fdB", off, (a.targetAra-a.targetPlain)/c)
	}
	fmt.Println()
}

// delayed is the source tau seconds later (an exact phase shift of its
// spectrum; the record is periodic, so nothing falls off the end).
func delayed(s Source, tau float64) Source {
	spec := make([]complex128, len(s.spec))
	for k := range spec {
		spec[k] = s.spec[k] * cmplx.Exp(complex(0, -2*math.Pi*binHz(k, len(spec))*tau))
	}
	return Source{spec}
}
