package beamformer

// Superdirective beams: minimum-variance weights against a diffuse noise field,
// fitted to short real FIRs.
//
// Delay-and-sum at a 72mm aperture has almost nothing to cancel below 1.5kHz
// because a diffuse field is nearly the same at every mic there (coherence
// 0.84-0.99). A minimum-variance design uses exactly that coherence: it picks
// weights that pass the look direction undistorted and minimise the power of
// an isotropic noise field, which at low frequency means differencing the mics.
// The price is white-noise gain: a differencing sum amplifies each mic's own
// noise and any gain or phase mismatch between mics. So the loading is chosen
// per frequency as the smallest that keeps the white-noise gain at or above a
// floor (SDMinWNGdB), and the mics are level-matched (calibration.go) first.
//
// The maths is textbook (Cox, Zeskind and Owen 1987; Bitzer and Simmer 2001)
// and the geometry is ours; nothing is taken from any vendor's table.

import (
	"math"
	"math/cmplx"
)

const (
	// SDFloorOffsetDB shifts sdFloorDB's profile. 0 follows stock's measured
	// white-noise gain; the design was checked in tools/bf_design with all
	// all seven mics and with the four stock uses.
	SDFloorOffsetDB = 0.0
	// SDTaps is each mic's FIR length: 2ms, the latency stock's design quotes.
	SDTaps = 32
	// sdFitHi is the top of the band the FIRs are fitted over. Speech and the
	// wake word's features end well below it, and asking a short filter to
	// follow the weights up to Nyquist spends taps on a band nobody scores.
	sdFitHi = 5200.0
	// sdRefine is how many times the fit is corrected for its own look-gain
	// error.
	sdRefine = 3
	// sdHighWeight is the fit weight above sdFitHi.
	sdHighWeight = 0.15
	sdRingStep   = 10.0
)

// SDRingWeight is the power of the directional-interference ring relative to
// the diffuse field (0 = diffuse only), and SDSectorDeg the half-width of the
// look sector it leaves alone. Variables so tools/bf_design can sweep them.
//
// 3 and 50 are from the tools/bf_design sweep on the four stock mics. Against
// an interferer opposite the talker, with the ring off and on: the 500-1500Hz
// band +1.9 -> +4.0dB and 1.5-4kHz -4.2 -> -1.5dB, for about 1dB of
// directivity at 1-3kHz and no change to an interferer at 90° (which a diffuse
// model already handles). Past a weight of 3 the gain flattens.
var (
	SDRingWeight = 3.0
	SDSectorDeg  = 50.0
)

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

// sdGrid is the design frequency grid, in Hz.
func sdGrid() []float64 {
	var g []float64
	for f := 62.5; f < sampleRate/2-1; f += 62.5 {
		g = append(g, f)
	}
	return g
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
// steering vector d and diffuse coherence matrix gamma, loaded to the smallest
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

// fitFIR returns the real FIR of length taps whose response is the
// least-squares fit to target(f) e^{-j w delay} on the grid. Real h makes the
// objective sum |sum_k h_k e^{-jwk} - T|^2 reduce to A h = b with
// A_kl = sum cos(w(k-l)), b_k = sum Re(T e^{jwk}).
func fitFIR(taps int, delay float64, grid, weight []float64, target []complex128) []float64 {
	a := make([][]float64, taps)
	for i := range a {
		a[i] = make([]float64, taps+1)
	}
	for g, f := range grid {
		w := 2 * math.Pi * f / sampleRate
		t := target[g] * cmplx.Exp(complex(0, -w*delay))
		wt := weight[g]
		for k := 0; k < taps; k++ {
			for l := 0; l < taps; l++ {
				a[k][l] += wt * math.Cos(w*float64(k-l))
			}
			a[k][taps] += wt * real(t*cmplx.Exp(complex(0, w*float64(k))))
		}
	}
	for k := 0; k < taps; k++ {
		a[k][k] += 1e-9 * float64(len(grid))
	}
	return gaussJordan(a)
}

// DesignSuperdirective derives nBeams beams, evenly spaced from 0°, for mics at
// pos. Each is a per-mic real FIR of SDTaps taps delayed by SDTaps/2 samples
// (the bulk latency, shared by every beam, so a mic at the origin stands in for
// a beam with no direction by a delay of exactly that many samples).
func DesignSuperdirective(pos [][2]float64, nBeams int, floorOffsetDB float64) []Beam {
	return designSD(pos, nBeams, floorOffsetDB, SDTaps, sdFitHi)
}

func designSD(pos [][2]float64, nBeams int, floorOffsetDB float64, taps int, fitHi float64) []Beam {
	grid := sdGrid()
	// Every frequency up to Nyquist is fitted, because a short FIR left free
	// anywhere does not stay near zero there: with the band above fitHi
	// dropped the filters gained 66dB at 5.5-8kHz. Above fitHi the bins count
	// for less, so the fit spends its taps on the band the wake word scores.
	weight := make([]float64, len(grid))
	for g, f := range grid {
		weight[g] = 1
		if f > fitHi {
			weight[g] = sdHighWeight
		}
	}
	n := len(pos)
	diffuse := make([][][]float64, len(grid))
	for g, f := range grid {
		diffuse[g] = make([][]float64, n)
		for i := range diffuse[g] {
			diffuse[g][i] = make([]float64, n)
			for j := range diffuse[g][i] {
				diffuse[g][i][j] = diffuseCoherence(math.Hypot(pos[i][0]-pos[j][0], pos[i][1]-pos[j][1]), f)
			}
		}
	}
	delay := float64(taps / 2)
	steer := func(az, f float64) []complex128 {
		u := unitVec(az, 0)
		d := make([]complex128, n)
		for i, p := range pos {
			adv := (u[0]*p[0] + u[1]*p[1]) / speedOfSound
			d[i] = cmplx.Exp(complex(0, 2*math.Pi*f*adv))
		}
		return d
	}
	beams := make([]Beam, nBeams)
	for b := range beams {
		az := 360 * float64(b) / float64(nBeams)
		u := unitVec(az, 0)
		target := make([][]complex128, n) // [mic][grid]
		for i := range target {
			target[i] = make([]complex128, len(grid))
		}
		for g, f := range grid {
			r := make([][]complex128, n)
			for i := range r {
				r[i] = make([]complex128, n)
				for j := range r[i] {
					r[i][j] = complex(diffuse[g][i][j], 0)
				}
			}
			// Directional interference: a ring of equal sources round the
			// horizontal plane, outside the look sector, where a TV or a
			// speaker on a shelf sits. Its weight is relative to the diffuse
			// field, and it is what lets the beam suppress one loud source
			// that a diffuse model does not know about.
			if SDRingWeight > 0 {
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
							r[i][j] += complex(SDRingWeight/float64(cnt), 0) * d[i] * cmplx.Conj(d[j])
						}
					}
				}
			}
			w := mvdrWeights(r, steer(az, f), math.Pow(10, sdFloorDB(f, floorOffsetDB)/10))
			for i := range w {
				target[i][g] = cmplx.Conj(w[i])
			}
		}
		bm := Beam{AzimuthDeg: az, Bulk: delay, Delay: make([]float64, n), H: make([][]float64, n)}
		for i := range pos {
			bm.H[i] = fitFIR(taps, delay, grid, weight, target[i])
		}
		// The weights are distortionless; the short filters that stand in for
		// them are not quite. Measure what the fitted beam actually passes from
		// its look direction at each frequency, scale the targets by the
		// reciprocal and fit again, so the error is spent where the fit has
		// slack instead of in the talker's direction.
		for it := 0; it < sdRefine; it++ {
			for g, f := range grid {
				w := 2 * math.Pi * f
				var y complex128
				for i, p := range pos {
					adv := (u[0]*p[0] + u[1]*p[1]) / speedOfSound
					var h complex128
					for k, c := range bm.H[i] {
						h += complex(c, 0) * cmplx.Exp(complex(0, -w*float64(k)/sampleRate))
					}
					y += h * cmplx.Exp(complex(0, w*adv))
				}
				y *= cmplx.Exp(complex(0, w*delay/sampleRate))
				if m := cmplx.Abs(y); m > 0.25 && m < 4 {
					for i := range target {
						target[i][g] /= y
					}
				}
			}
			for i := range pos {
				bm.H[i] = fitFIR(taps, delay, grid, weight, target[i])
			}
		}
		beams[b] = bm
	}
	return beams
}
