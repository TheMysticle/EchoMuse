package main

import (
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

// Design derives nBeams beams with the firmware's design.
func Design(pos [][2]float64, nBeams, taps int, fLo, fHi float64) []Beam {
	if useSD {
		return beamformer.DesignSuperdirective(pos, nBeams, sdWNG)
	}
	return beamformer.DesignBeams(pos, nBeams, taps, fLo, fHi)
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
