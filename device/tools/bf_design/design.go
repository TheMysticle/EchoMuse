package main

import (
	"math"
	"math/cmplx"
)

// Speed of sound at ~20°C, and the capture rate every mic path in the device
// already runs at (internal/beamformer: sampleRate).
const (
	speedOfSound = 343.0
	sampleRate   = 16000.0
)

// Geometry mirrors internal/beamformer.micAngles (angle clockwise from
// 12 o'clock, ALSA channel order) and its 36mm perimeter radius. It is copied
// rather than imported because the package's table is unexported; if the board
// ever changes, change both. Ch0-5 perimeter, ch6 centre.
var micAnglesDeg = [7]float64{330, 30, 90, 150, 210, 270, 0}

const perimeterRadius = 0.036 // metres

// Mic sets the tool can design for. "amazon4" is ch0, ch1, ch3, ch4 (330°,
// 30°, 150°, 210°): the four mics Radar's AFE.cfg selects for its wake path
// (`Channel Map [0,1,3,4]`; the beamformer file calls them mics 1,2,4,5,
// 1-indexed). On this board that is a 36 x 62mm rectangle, not a square.
var micSets = map[string][]int{
	"all":       {0, 1, 2, 3, 4, 5, 6},
	"perimeter": {0, 1, 2, 3, 4, 5},
	"amazon4":   {0, 1, 3, 4},
}

// micPositions returns x (right) and y (towards 12 o'clock) in metres for the
// named set. A compass angle a puts a point at (r sin a, r cos a).
func micPositions(set string) [][2]float64 {
	var p [][2]float64
	for _, i := range micSets[set] {
		r := perimeterRadius
		if i == 6 {
			r = 0
		}
		rad := micAnglesDeg[i] * math.Pi / 180
		p = append(p, [2]float64{r * math.Sin(rad), r * math.Cos(rad)})
	}
	return p
}

// Beam is one filter-and-sum beam: an FIR per microphone, summed.
type Beam struct {
	AzimuthDeg float64
	// H[mic] is that mic's FIR. Each is a 4-tap fractional-delay interpolator
	// placed at its integer delay, so a beam's longest filter is only a few
	// samples; zero elsewhere.
	H [][]float64
	// DelaySamples[mic] is the delay that filter was designed for.
	DelaySamples []float64
	// Bulk is the delay every mic's path has in common at the look direction
	// (the offset that makes all delays causal). It is latency, not error.
	Bulk float64
}

// Design derives nBeams beams, evenly spaced from 0°, for the given mic
// positions. Each mic is delayed by its projection on the look direction
// (plane wave from azimuth a: mic i is reached (u·p_i)/c seconds earlier than
// the origin, so it is delayed by that much to line the wavefronts up), plus a
// common offset that makes the smallest delay exactly one sample so every tap
// index is causal. Weights are 1/M: unity gain at the look direction.
//
// taps is the length of each fractional-delay interpolator. The delay spread
// across a 72mm aperture is 2*0.036/343*16000 = 3.4 samples, so the design
// needs integer alignment on top of the interpolator; that is where the
// extra length comes from.
func Design(pos [][2]float64, nBeams, taps int, fLo, fHi float64) []Beam {
	beams := make([]Beam, nBeams)
	for b := range beams {
		az := 360 * float64(b) / float64(nBeams)
		beams[b] = designBeam(pos, az, taps, fLo, fHi)
	}
	return beams
}

func unit(azDeg, elDeg float64) [3]float64 {
	a, e := azDeg*math.Pi/180, elDeg*math.Pi/180
	return [3]float64{math.Sin(a) * math.Cos(e), math.Cos(a) * math.Cos(e), math.Sin(e)}
}

func designBeam(pos [][2]float64, azDeg float64, taps int, fLo, fHi float64) Beam {
	u := unit(azDeg, 0)
	d := make([]float64, len(pos))
	minD := math.Inf(1)
	for i, p := range pos {
		d[i] = (u[0]*p[0] + u[1]*p[1]) / speedOfSound * sampleRate
		minD = math.Min(minD, d[i])
	}
	// Shift so the smallest delay is (taps/2 - 1) + 0 fractional: interpolation
	// is best with the target delay in the middle of the tap window, and the
	// window's first tap must not go below index 0.
	lead := float64(taps/2 - 1)
	off := lead - minD
	b := Beam{AzimuthDeg: azDeg, DelaySamples: make([]float64, len(pos)), Bulk: off}
	maxLen := 0
	hs := make([][]float64, len(pos))
	starts := make([]int, len(pos))
	for i := range pos {
		di := d[i] + off
		b.DelaySamples[i] = di
		n := int(math.Floor(di))
		frac := di - float64(n)
		hs[i] = fractionalDelayLS(taps, lead+frac, fLo, fHi)
		starts[i] = n - int(lead)
		if e := starts[i] + taps; e > maxLen {
			maxLen = e
		}
	}
	w := 1.0 / float64(len(pos))
	b.H = make([][]float64, len(pos))
	for i := range pos {
		b.H[i] = make([]float64, maxLen)
		for k, v := range hs[i] {
			b.H[i][starts[i]+k] = v * w
		}
	}
	return b
}

// fractionalDelayLS returns taps coefficients whose frequency response is the
// least-squares fit of a pure delay of delay samples (taps/2-1 <= delay <=
// taps/2) over [fLo, fHi]. Real h makes the objective sum_w |sum_k h_k e^{-jwk}
// - e^{-jw delay}|^2 reduce to A h = b with A_kl = sum cos(w(k-l)) and
// b_k = sum cos(w(k-delay)).
func fractionalDelayLS(taps int, delay, fLo, fHi float64) []float64 {
	const grid = 400
	A := make([][]float64, taps)
	for i := range A {
		A[i] = make([]float64, taps+1) // last column: b
	}
	for g := 0; g < grid; g++ {
		f := fLo + (fHi-fLo)*(float64(g)+0.5)/grid
		w := 2 * math.Pi * f / sampleRate
		for k := 0; k < taps; k++ {
			for l := 0; l < taps; l++ {
				A[k][l] += math.Cos(w * float64(k-l))
			}
			A[k][taps] += math.Cos(w * (float64(k) - delay))
		}
	}
	return solve(A)
}

// solve does Gauss-Jordan with partial pivoting on an n x (n+1) system.
func solve(A [][]float64) []float64 {
	n := len(A)
	for c := 0; c < n; c++ {
		p := c
		for r := c + 1; r < n; r++ {
			if math.Abs(A[r][c]) > math.Abs(A[p][c]) {
				p = r
			}
		}
		A[c], A[p] = A[p], A[c]
		for r := 0; r < n; r++ {
			if r == c {
				continue
			}
			m := A[r][c] / A[c][c]
			for k := c; k <= n; k++ {
				A[r][k] -= m * A[c][k]
			}
		}
	}
	x := make([]float64, n)
	for i := range x {
		x[i] = A[i][n] / A[i][i]
	}
	return x
}

// Response is the beam's complex gain for a plane wave of frequency f arriving
// from (azDeg, elDeg). Mic i sees the wave advanced by (u·p_i)/c.
func (b Beam) Response(pos [][2]float64, f, azDeg, elDeg float64) complex128 {
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
func (b Beam) lookGain(pos [][2]float64, f float64) complex128 {
	y := b.Response(pos, f, b.AzimuthDeg, 0)
	return y * cmplx.Exp(complex(0, 2*math.Pi*f*b.Bulk/sampleRate))
}
