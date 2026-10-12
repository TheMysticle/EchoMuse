package main

import (
	"fmt"
	"math"
	"math/cmplx"

	"github.com/wilbowes/EchoMuse/internal/beamformer"
)

// Speed of sound at ~20°C, and the capture rate every mic path in the device
// already runs at (internal/beamformer: sampleRate).
const (
	speedOfSound = 343.0
	sampleRate   = 16000.0
)

// Beam is the firmware's own: this tool measures the code that ships
// (internal/beamformer/fbf.go), not a copy of it.
type Beam = beamformer.Beam

// Mic sets the tool can design for, as ALSA channels. "amazon4" is ch0, ch1,
// ch3, ch4 (330°, 30°, 150°, 210°): the four mics Radar's AFE.cfg selects for
// its wake path (`Channel Map [0,1,3,4]`; the beamformer file calls them mics
// 1,2,4,5, 1-indexed). On this board that is a 36 x 62mm rectangle, not a
// square.
var micSets = map[string][]int{
	"all":       {0, 1, 2, 3, 4, 5, 6},
	"perimeter": {0, 1, 2, 3, 4, 5},
	"amazon4":   {0, 1, 3, 4},
}

// micPositions returns x (right) and y (towards 12 o'clock) in metres for the
// named set, from the firmware's geometry.
func micPositions(set string) [][2]float64 {
	var p [][2]float64
	for _, ch := range micSets[set] {
		p = append(p, beamformer.MicPosition(ch))
	}
	return p
}

// useSD and sdWNG pick the design: delay-and-sum, or the superdirective one
// with a white-noise-gain floor in dB.
var (
	useSD bool
	sdWNG = beamformer.SDFloorOffsetDB
)

// Design derives nBeams delay-and-sum beams with the firmware's baseline design.
func Design(pos [][2]float64, nBeams, taps int, fLo, fHi float64) []Beam {
	return beamformer.DesignBeams(pos, nBeams, taps, fLo, fHi)
}

// designSet is a set of beams the tool can measure: the delay-and-sum baseline
// (time-domain FIRs) or the firmware's superdirective beams (per-bin weights
// applied to the mics' spectra, inside the canceller stage). Every report asks
// it the same few questions, so it measures whichever ships.
type designSet struct {
	pos [][2]float64
	das []Beam
	sd  *beamformer.SDWeights

	memoKey *float64
	memo    [][]float32
}

func newDesignSet(pos [][2]float64, nBeams, taps int, fLo, fHi float64) *designSet {
	d := &designSet{pos: pos}
	if useSD {
		d.sd = beamformer.DesignSuperdirective(pos, nBeams, sdWNG)
	} else {
		d.das = Design(pos, nBeams, taps, fLo, fHi)
	}
	return d
}

func (d *designSet) n() int {
	if d.sd != nil {
		return d.sd.Beams()
	}
	return len(d.das)
}

// az is beam b's look direction.
func (d *designSet) az(b int) float64 {
	if d.sd != nil {
		return d.sd.Azimuths[b]
	}
	return d.das[b].AzimuthDeg
}

// resp is beam b's complex gain for a plane wave of frequency f from (az, el).
func (d *designSet) resp(b int, f, az, el float64) complex128 {
	if d.sd != nil {
		return d.sd.Response(b, f, az, el)
	}
	return response(d.das[b], d.pos, f, az, el)
}

// noise is beam b's gain for noise independent between mics at f: the sum of
// the squared per-mic gains.
func (d *designSet) noise(b int, f float64) float64 {
	if d.sd != nil {
		k := int(math.Round(f * 256 / sampleRate))
		return d.sd.NoisePower(b, k)
	}
	var s float64
	for i := range d.pos {
		var h complex128
		for t, c := range d.das[b].H[i] {
			h += complex(c*math.Cos(2*math.Pi*f*float64(t)/sampleRate), -c*math.Sin(2*math.Pi*f*float64(t)/sampleRate))
		}
		s += real(h)*real(h) + imag(h)*imag(h)
	}
	return s
}

// look is beam b's gain at its own look direction, any common delay removed.
func (d *designSet) look(b int, f float64) complex128 {
	if d.sd != nil {
		return d.sd.Response(b, f, d.sd.Azimuths[b], 0)
	}
	return lookGain(d.das[b], d.pos, f)
}

// describe is one line about the design for the report header.
func (d *designSet) describe() string {
	if d.sd != nil {
		return fmt.Sprintf("%d superdirective beams, per-bin weights over %d mics, look directions %v", d.sd.Beams(), len(d.pos), d.sd.Azimuths)
	}
	return fmt.Sprintf("%d delay-and-sum beams, longest filter %d samples, latency %.2f samples", len(d.das), len(d.das[0].H[0]), d.das[0].Bulk)
}

// apply is beam b's time-domain output for the mic signals, up to a fixed delay.
func (d *designSet) apply(sig [][]float64, b int) []float64 {
	if d.das != nil {
		return apply(d.das[b], sig)
	}
	out := d.sdOutputs(sig)
	y := make([]float64, len(out[b]))
	for i, v := range out[b] {
		y[i] = float64(v)
	}
	return y
}

// sdOutputs forms every beam from the mic signals through the firmware's own
// stage with the canceller off, and remembers the last signal it did.
func (d *designSet) sdOutputs(sig [][]float64) [][]float32 {
	if d.memoKey == &sig[0][0] {
		return d.memo
	}
	d.memoKey, d.memo = &sig[0][0], formBeams(d.sd, sig, false)
	return d.memo
}

// formBeams runs the mic signals through the firmware's stage in the batches
// the mic loop delivers, with the canceller on or off.
func formBeams(sd *beamformer.SDWeights, sig [][]float64, ara bool) [][]float32 {
	n := len(sig[0])
	mics := make([][]float32, len(sig))
	for m := range sig {
		mics[m] = make([]float32, n)
		for i, v := range sig[m] {
			mics[m][i] = float32(v)
		}
	}
	a := beamformer.NewARA(sd.Beams())
	a.SetEnabled(ara)
	out := make([][]float32, sd.Beams())
	for b := range out {
		out[b] = make([]float32, n)
	}
	const call = 2560
	for s := 0; s < n; s += call {
		e := min(s+call, n)
		in := make([][]float32, len(mics))
		o := make([][]float32, len(out))
		for m := range mics {
			in[m] = mics[m][s:e]
		}
		for b := range out {
			o[b] = out[b][s:e]
		}
		a.ProcessMics(in, sd, o)
	}
	return out
}

func unit(azDeg, elDeg float64) [3]float64 {
	a, e := azDeg*math.Pi/180, elDeg*math.Pi/180
	return [3]float64{math.Sin(a) * math.Cos(e), math.Cos(a) * math.Cos(e), math.Sin(e)}
}

// response is the beam's complex gain for a plane wave of frequency f arriving
// from (azDeg, elDeg). Mic i sees the wave advanced by (u·p_i)/c.
func response(b Beam, pos [][2]float64, f, azDeg, elDeg float64) complex128 {
	u := unit(azDeg, elDeg)
	w := 2 * math.Pi * f
	var y complex128
	for i, p := range pos {
		adv := (u[0]*p[0] + u[1]*p[1]) / speedOfSound
		x := cmplx.Exp(complex(0, w*adv))
		var h complex128
		for t, c := range b.H[i] {
			h += complex(c, 0) * cmplx.Exp(complex(0, -w*float64(t)/sampleRate))
		}
		y += h * x
	}
	return y
}

// lookGain is the response at the beam's own azimuth with the common bulk
// delay divided out, so a perfect beam reads 1 (0dB, zero phase).
func lookGain(b Beam, pos [][2]float64, f float64) complex128 {
	y := response(b, pos, f, b.AzimuthDeg, 0)
	return y * cmplx.Exp(complex(0, 2*math.Pi*f*b.Bulk/sampleRate))
}

// apply runs the beam in the time domain: y[t] = sum_i sum_k H[i][k] x_i[t-k].
// Circular, because the sources are periodic over the record.
func apply(b Beam, x [][]float64) []float64 {
	n := len(x[0])
	y := make([]float64, n)
	for i, h := range b.H {
		for k, c := range h {
			if c == 0 {
				continue
			}
			for t := 0; t < n; t++ {
				y[t] += c * x[i][((t-k)%n+n)%n]
			}
		}
	}
	return y
}
