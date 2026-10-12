package beamformer

// Superdirective beams: minimum-variance weights against a noise field, one
// complex weight per microphone per 62.5Hz bin.
//
// Delay-and-sum at a 72mm aperture has almost nothing to cancel below 1.5kHz
// because a diffuse field is nearly the same at every mic there (coherence
// 0.84-0.99). A minimum-variance design uses exactly that coherence: it picks
// weights that pass the look direction undistorted and minimise the power of
// the noise field, which at low frequency means differencing the mics. The
// price is white-noise gain: a differencing sum amplifies each mic's own noise
// and any gain or phase mismatch between mics. So the loading is chosen per
// frequency as the smallest that keeps the white-noise gain above a floor
// (sdFloorDB), and the mics are level-matched (miccal.go) first.
//
// The weights are applied to the mics' short-time spectra (ara.go), in the same
// 256-point, 62.5Hz-bin domain Radar's stock beamformer works in, so they ARE
// the design: nothing is fitted to a time-domain filter, the look direction is
// passed at exactly unity, and the resolution is not limited by a filter
// length. A first version fitted 32 and then 64-tap FIRs; at 32 the directivity
// at 250Hz was 2.8dB against stock's 4.5, at 64 it matched and the cost
// doubled, and the cost of the weights is 16 times less.
//
// The noise model is a diffuse field plus a ring of directional interferers
// outside the look sector (SDRingWeight, SDSectorDeg), and the beams look in
// stock's arrangement for these four mics (SDAzimuths). Each choice was set by
// measuring against stock's own beams, with tools/bf_design -amazon.
//
// The maths is textbook (Cox, Zeskind and Owen 1987; Bitzer and Simmer 2001)
// and the geometry is ours; nothing is taken from any vendor's table.

import (
	"math"
	"math/cmplx"
)

// Tunables, variables so tools/bf_design can sweep them.
var (
	// SDFloorOffsetDB shifts sdFloorDB's profile. 0 follows stock's measured
	// white-noise gain, which this design reproduces to within about 1dB per
	// beam at 250Hz, 500Hz, 1kHz and 2kHz.
	SDFloorOffsetDB = 0.0
	// SDRingWeight is the power of the directional-interference ring relative to
	// the diffuse field (0 = diffuse only), and SDSectorDeg the half-width of the
	// look sector it leaves alone. Against an interferer opposite the talker the
	// ring takes the 500-1500Hz band from +1.9 to +4.0dB and 1.5-4kHz from -4.2 to
	// -1.5dB (before ARA), for about 1dB of directivity at 1-3kHz. Past a weight
	// of 3 the gain flattens.
	SDRingWeight = 3.0
	SDSectorDeg  = 35.0
	// SDRingLowHz tapers the ring's weight linearly to zero below this
	// frequency (0 = no taper). It made no difference to the 250Hz directivity,
	// so it stays off.
	SDRingLowHz = 0.0
	// SDAzimuths, when its length is the number of beams, replaces the even
	// spacing with these look directions (degrees, clockwise from 12 o'clock).
	// The default is stock's arrangement for these four mics: its eight beams
	// sit in four pairs about 22 degrees apart, peaking near 316/292, 236/215,
	// 136/112 and 56/35, not on a 45 degree grid (tools/bf_design -amazon). In
	// stock's index order, so the ARA reference table (beam i uses i+3 and i+5)
	// means the same thing. They are the design angles that make a beam of this
	// design peak where stock's does: a beam's peak sits a few degrees off its
	// look direction.
	SDAzimuths = []float64{322, 289, 235, 205, 142, 110, 55, 25}
)

const sdRingStep = 10.0

// SDWeights is a set of beams: per-bin complex weights over the microphones.
type SDWeights struct {
	// Azimuths are the look directions, degrees clockwise from 12 o'clock.
	Azimuths []float64
	// Mics is the number of microphones summed.
	Mics int
	// W[b][m][k] is microphone m's weight in beam b at bin k, so that the beam's
	// spectrum is the sum over m of W[b][m][k] times the mic's. Bins 0 and
	// araBins-1 (DC and Nyquist) are zero.
	W [][][araBins]complex64

	pos [][2]float64
}

// Beams is the number of beams.
func (s *SDWeights) Beams() int { return len(s.W) }

// Response is beam b's complex gain for a plane wave of frequency f from
// (azDeg, elDeg), using the weights of the bin nearest f. It is exact at the
// bin centres (multiples of 62.5Hz) and an approximation between them.
func (s *SDWeights) Response(b int, f, azDeg, elDeg float64) complex128 {
	// f * araFFT / sampleRate, not f / (sampleRate / araFFT): both are untyped
	// integer constants, so the bracket is 62 and not 62.5.
	k := int(math.Round(f * araFFT / sampleRate))
	if k < 1 || k > araBins-2 {
		return 0
	}
	u := unitVec(azDeg, elDeg)
	var y complex128
	for m, p := range s.pos {
		adv := (u[0]*p[0] + u[1]*p[1]) / speedOfSound
		w := s.W[b][m][k]
		y += complex(float64(real(w)), float64(imag(w))) * cmplx.Exp(complex(0, 2*math.Pi*f*adv))
	}
	return y
}

// NoisePower is beam b's gain for noise independent between mics at bin k: the
// sum of the squared weights. The white-noise gain is its reciprocal over the
// look-direction power, which is 1.
func (s *SDWeights) NoisePower(b, k int) float64 {
	var p float64
	for m := range s.pos {
		w := s.W[b][m][k]
		p += float64(real(w))*float64(real(w)) + float64(imag(w))*float64(imag(w))
	}
	return p
}

// sdFloorDB is the white-noise-gain floor at f, in dB. It is a profile and not
// one number because the cost and the benefit both change with frequency:
// differencing four mics 36mm apart is worth a lot at 250Hz, where a sum does
// nothing, and almost nothing above 2kHz, where a sum already works. Radar's
// stock beams measure -7.4dB at 250Hz, -4.0 at 500, -1.5 at 1kHz and positive
// from 2kHz (tools/bf_design -amazon); this follows those four points, linear
// in log frequency and flat outside them. offsetDB shifts the whole profile
// (more negative buys directivity and sensitivity to mic mismatch).
func sdFloorDB(f, offsetDB float64) float64 {
	pts := [][2]float64{{250, -7}, {500, -4}, {1000, -1.5}, {2000, 0}}
	switch {
	case f <= pts[0][0]:
		return pts[0][1] + offsetDB
	case f >= pts[len(pts)-1][0]:
		return pts[len(pts)-1][1] + offsetDB
	}
	for i := 1; i < len(pts); i++ {
		if f <= pts[i][0] {
			t := math.Log2(f/pts[i-1][0]) / math.Log2(pts[i][0]/pts[i-1][0])
			return pts[i-1][1] + t*(pts[i][1]-pts[i-1][1]) + offsetDB
		}
	}
	return 0
}

// diffuseCoherence is the isotropic-field coherence between two mics d apart
// at f: sin(kd)/kd.
func diffuseCoherence(d, f float64) float64 {
	x := 2 * math.Pi * f * d / speedOfSound
	if x < 1e-9 {
		return 1
	}
	return math.Sin(x) / x
}

// mvdrWeights returns the weights w (so that output = sum conj(w_m) x_m) for
// steering vector d and noise covariance gamma, loaded to the smallest
// diagonal that keeps 1/(w^H w) at or above minWNG (linear). Distortionless:
// w^H d = 1.
func mvdrWeights(gamma [][]complex128, d []complex128, minWNG float64) []complex128 {
	n := len(d)
	solve := func(eps float64) []complex128 {
		a := make([][]complex128, n)
		for i := range a {
			a[i] = make([]complex128, n+1)
			for j := 0; j < n; j++ {
				a[i][j] = gamma[i][j]
			}
			a[i][i] += complex(eps, 0)
			a[i][n] = d[i]
		}
		x := gaussJordanC(a)
		var den complex128
		for i := range x {
			den += cmplx.Conj(d[i]) * x[i]
		}
		w := make([]complex128, n)
		for i := range w {
			w[i] = x[i] / den
		}
		return w
	}
	wng := func(w []complex128) float64 {
		var s float64
		for _, v := range w {
			s += real(v)*real(v) + imag(v)*imag(v)
		}
		return 1 / s
	}
	// Smallest loading meeting the floor: the WNG rises with eps, so bisect in
	// the log domain between "almost none" and "so much it is delay-and-sum".
	lo, hi := math.Log(1e-6), math.Log(1e3)
	if w := solve(math.Exp(lo)); wng(w) >= minWNG {
		return w
	}
	for it := 0; it < 16; it++ {
		mid := (lo + hi) / 2
		if wng(solve(math.Exp(mid))) >= minWNG {
			hi = mid
		} else {
			lo = mid
		}
	}
	return solve(math.Exp(hi))
}

// gaussJordanC solves an n x (n+1) complex system with partial pivoting.
func gaussJordanC(a [][]complex128) []complex128 {
	n := len(a)
	for c := 0; c < n; c++ {
		p := c
		for r := c + 1; r < n; r++ {
			if cmplx.Abs(a[r][c]) > cmplx.Abs(a[p][c]) {
				p = r
			}
		}
		a[c], a[p] = a[p], a[c]
		for r := 0; r < n; r++ {
			if r == c {
				continue
			}
			m := a[r][c] / a[c][c]
			for k := c; k <= n; k++ {
				a[r][k] -= m * a[c][k]
			}
		}
	}
	x := make([]complex128, n)
	for i := range x {
		x[i] = a[i][n] / a[i][i]
	}
	return x
}

func sdAzimuth(b, nBeams int) float64 {
	if len(SDAzimuths) == nBeams {
		return SDAzimuths[b]
	}
	return 360 * float64(b) / float64(nBeams)
}

// DesignSuperdirective derives nBeams beams for mics at pos: at each of the
// filter bank's bins, minimum-variance weights against the diffuse field plus
// the interference ring, loaded to the white-noise-gain floor.
func DesignSuperdirective(pos [][2]float64, nBeams int, floorOffsetDB float64) *SDWeights {
	n := len(pos)
	out := &SDWeights{Mics: n, pos: pos, Azimuths: make([]float64, nBeams), W: make([][][araBins]complex64, nBeams)}
	for b := range out.W {
		out.W[b] = make([][araBins]complex64, n)
		out.Azimuths[b] = sdAzimuth(b, nBeams)
	}
	steer := func(az, f float64) []complex128 {
		u := unitVec(az, 0)
		d := make([]complex128, n)
		for i, p := range pos {
			adv := (u[0]*p[0] + u[1]*p[1]) / speedOfSound
			d[i] = cmplx.Exp(complex(0, 2*math.Pi*f*adv))
		}
		return d
	}
	for k := 1; k < araBins-1; k++ {
		f := float64(k) * sampleRate / araFFT
		diffuse := make([][]complex128, n)
		for i := range diffuse {
			diffuse[i] = make([]complex128, n)
			for j := range diffuse[i] {
				diffuse[i][j] = complex(diffuseCoherence(math.Hypot(pos[i][0]-pos[j][0], pos[i][1]-pos[j][1]), f), 0)
			}
		}
		minWNG := math.Pow(10, sdFloorDB(f, floorOffsetDB)/10)
		for b := 0; b < nBeams; b++ {
			az := out.Azimuths[b]
			r := make([][]complex128, n)
			for i := range r {
				r[i] = append([]complex128(nil), diffuse[i]...)
			}
			// Directional interference: a ring of equal sources round the
			// horizontal plane, outside the look sector, where a TV or a speaker
			// on a shelf sits. Its weight is relative to the diffuse field, and
			// it is what lets the beam suppress one loud source that a diffuse
			// model does not know about.
			ringW := SDRingWeight
			if SDRingLowHz > 0 && f < SDRingLowHz {
				ringW *= f / SDRingLowHz
			}
			if ringW > 0 {
				var cnt int
				for th := 0.0; th < 360; th += sdRingStep {
					if math.Abs(angleDiff(th, az)) > SDSectorDeg {
						cnt++
					}
				}
				for th := 0.0; th < 360; th += sdRingStep {
					if math.Abs(angleDiff(th, az)) <= SDSectorDeg {
						continue
					}
					d := steer(th, f)
					for i := range r {
						for j := range r[i] {
							r[i][j] += complex(ringW/float64(cnt), 0) * d[i] * cmplx.Conj(d[j])
						}
					}
				}
			}
			w := mvdrWeights(r, steer(az, f), minWNG)
			for m := range w {
				c := cmplx.Conj(w[m])
				out.W[b][m][k] = complex(float32(real(c)), float32(imag(c)))
			}
		}
	}
	return out
}
