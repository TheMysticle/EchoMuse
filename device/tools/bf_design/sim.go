package main

import (
	"math"
	"math/cmplx"
	"math/rand"
)

// fft is an in-place iterative radix-2 transform; inverse scales by 1/n.
func fft(x []complex128, inverse bool) {
	n := len(x)
	for i, j := 1, 0; i < n; i++ {
		bit := n >> 1
		for ; j&bit != 0; bit >>= 1 {
			j ^= bit
		}
		j ^= bit
		if i < j {
			x[i], x[j] = x[j], x[i]
		}
	}
	for l := 2; l <= n; l <<= 1 {
		ang := -2 * math.Pi / float64(l)
		if inverse {
			ang = -ang
		}
		wl := cmplx.Exp(complex(0, ang))
		for i := 0; i < n; i += l {
			w := complex(1, 0)
			for k := 0; k < l/2; k++ {
				u, v := x[i+k], x[i+k+l/2]*w
				x[i+k], x[i+k+l/2] = u+v, u-v
				w *= wl
			}
		}
	}
	if inverse {
		for i := range x {
			x[i] /= complex(float64(n), 0)
		}
	}
}

// binHz is the signed frequency of FFT bin k.
func binHz(k, n int) float64 {
	if k > n/2 {
		k -= n
	}
	return float64(k) * sampleRate / float64(n)
}

// Source is a stationary Gaussian signal with a given magnitude shape,
// held as its spectrum so it can be delayed exactly per microphone.
type Source struct{ spec []complex128 }

// NewSource draws white Gaussian noise and shapes it by shape(f) (an
// amplitude, in Hz), band-limited to [lo, hi]. The result is scaled so that
// its power inside [lo, hi] is 1.
func NewSource(rng *rand.Rand, n int, lo, hi float64, shape func(f float64) float64) Source {
	x := make([]complex128, n)
	for i := range x {
		x[i] = complex(rng.NormFloat64(), 0)
	}
	fft(x, false)
	var p float64
	for k := range x {
		f := math.Abs(binHz(k, n))
		if f < lo || f > hi || k == n/2 {
			x[k] = 0
			continue
		}
		x[k] *= complex(shape(f), 0)
		p += real(x[k])*real(x[k]) + imag(x[k])*imag(x[k])
	}
	// Parseval: mean-square of the time signal is p / n^2.
	g := 1 / math.Sqrt(p/float64(n)/float64(n))
	for k := range x {
		x[k] *= complex(g, 0)
	}
	return Source{x}
}

// AtMics returns the time signal at each mic for a plane wave from (az, el):
// the source advanced by (u·p_i)/c, applied as an exact phase shift.
func (s Source) AtMics(pos [][2]float64, az, el float64) [][]float64 {
	n := len(s.spec)
	u := unit(az, el)
	out := make([][]float64, len(pos))
	for i, p := range pos {
		adv := (u[0]*p[0] + u[1]*p[1]) / speedOfSound
		y := make([]complex128, n)
		for k := range y {
			w := 2 * math.Pi * binHz(k, n)
			y[k] = s.spec[k] * cmplx.Exp(complex(0, w*adv))
		}
		fft(y, true)
		out[i] = make([]float64, n)
		for t := range y {
			out[i][t] = real(y[t])
		}
	}
	return out
}

// speechShape is a long-term speech-like spectrum: flat to 500Hz, then
// -6dB/octave. A proxy, not a recording; the tool has no voice corpus.
func speechShape(f float64) float64 {
	if f <= 500 {
		return 1
	}
	return 500 / f
}

// musicShape is deliberately flatter than speech (roughly -3dB/octave from
// 200Hz), standing in for music or TV that has energy where speech does not.
func musicShape(f float64) float64 {
	if f <= 200 {
		return 1
	}
	return math.Sqrt(200 / f)
}

// flat is the shape of microphone self-noise.
func flat(float64) float64 { return 1 }

// fibonacciSphere returns n near-uniform (az, el) directions in degrees.
func fibonacciSphere(n int) [][2]float64 {
	g := math.Pi * (3 - math.Sqrt(5))
	d := make([][2]float64, n)
	for i := range d {
		z := 1 - 2*(float64(i)+0.5)/float64(n)
		d[i] = [2]float64{math.Mod(float64(i)*g*180/math.Pi, 360), math.Asin(z) * 180 / math.Pi}
	}
	return d
}

// Diffuse builds the mic signals of an isotropic field: nDir independent
// speech-shaped sources spread over the sphere (spatially white sources with
// the mic geometry's own delays, which is what gives the field its coherence).
// Power per mic is normalised to 1 inside the band.
func Diffuse(rng *rand.Rand, pos [][2]float64, n, nDir int, lo, hi float64) [][]float64 {
	out := make([][]float64, len(pos))
	for i := range out {
		out[i] = make([]float64, n)
	}
	for _, d := range fibonacciSphere(nDir) {
		s := NewSource(rng, n, lo, hi, speechShape)
		for i, sig := range s.AtMics(pos, d[0], d[1]) {
			for t, v := range sig {
				out[i][t] += v / math.Sqrt(float64(nDir))
			}
		}
	}
	return out
}

// Time returns the source as a time signal.
func (s Source) Time() []float64 {
	y := append([]complex128(nil), s.spec...)
	fft(y, true)
	out := make([]float64, len(y))
	for i, v := range y {
		out[i] = real(v)
	}
	return out
}

// SelfNoise is independent white noise per mic with unit in-band power.
func SelfNoise(rng *rand.Rand, nMics, n int, lo, hi float64) [][]float64 {
	out := make([][]float64, nMics)
	for i := range out {
		out[i] = NewSource(rng, n, lo, hi, flat).Time()
	}
	return out
}

// scale multiplies every mic signal by g (an amplitude).
func scale(x [][]float64, g float64) [][]float64 {
	out := make([][]float64, len(x))
	for i := range x {
		out[i] = make([]float64, len(x[i]))
		for t, v := range x[i] {
			out[i][t] = v * g
		}
	}
	return out
}

// bandPower is the mean-square of x inside [lo, hi] Hz, by Parseval.
func bandPower(x []float64, lo, hi float64) float64 {
	n := len(x)
	c := make([]complex128, n)
	for i, v := range x {
		c[i] = complex(v, 0)
	}
	fft(c, false)
	var p float64
	for k := range c {
		f := math.Abs(binHz(k, n))
		if f >= lo && f <= hi && k != n/2 {
			p += real(c[k])*real(c[k]) + imag(c[k])*imag(c[k])
		}
	}
	return p / float64(n) / float64(n)
}

func db(r float64) float64 { return 10 * math.Log10(r) }
