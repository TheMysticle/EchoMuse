package main

// Does stock's beam table work better in stock's own filter bank than folded
// into one weight per bin of ours?
//
// The table is 4 taps per band across frames of the AFE's 768-tap, hop-128,
// 256-point bank (coefs_FilterBank_AnalysisSynthesis_768cvxGLow.cfg). The
// firmware folds the taps into one weight per bin, exact only at the bin
// centres. This builds the bank, checks it reconstructs, checks the table in
// it points where it should, then runs a real capture through four routes and
// compares the eight beam groups of each by voice over background and tilt:
//
//	bank + taps     the table as stock applies it
//	bank + fold     the same table folded, in the same bank (the taps alone)
//	ours + table    the firmware's path with the table folded (what ships now)
//	ours + design   the firmware's path with our own beams
//
// Nothing of the table or the bank is stored: both are read from the paths
// given.

import (
	"bufio"
	"fmt"
	"math"
	"math/cmplx"
	"math/rand"
	"os"
	"regexp"
	"strconv"

	"github.com/wilbowes/EchoMuse/internal/beamformer"
)

const (
	bkN   = 256
	bkH   = 128
	bkLen = 768
)

type bank struct{ p [bkLen]float64 }

func loadBank(path string) (*bank, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	re := regexp.MustCompile(`^\s*(-?\d+\.\d+(?:[eE][-+]?\d+)?),?\s*$`)
	var v []float64
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if m := re.FindStringSubmatch(sc.Text()); m != nil {
			x, _ := strconv.ParseFloat(m[1], 64)
			v = append(v, x)
		}
	}
	if len(v) != bkLen {
		return nil, fmt.Errorf("%d values, want %d", len(v), bkLen)
	}
	b := &bank{}
	copy(b.p[:], v)
	return b, nil
}

// analyse returns frames[n][k], k = 0..128, frame n ending at sample n*H+H-1
// (so the first frame sees 128 samples of signal and zeros before).
func (b *bank) analyse(x []float64) [][]complex128 {
	nf := len(x) / bkH
	out := make([][]complex128, nf)
	z := make([]complex128, bkN)
	for n := 0; n < nf; n++ {
		for r := range z {
			z[r] = 0
		}
		end := n*bkH + bkH - 1
		for i := 0; i < bkLen; i++ {
			j := end - i
			if j < 0 {
				break
			}
			z[i%bkN] += complex(b.p[i]*x[j], 0)
		}
		fft(z, false)
		out[n] = append([]complex128(nil), z[:bkN/2+1]...)
	}
	return out
}

// synth inverts analyse; the result is delayed by the bank's own latency.
func (b *bank) synth(fr [][]complex128, n int) []float64 {
	y := make([]float64, n+bkLen)
	z := make([]complex128, bkN)
	for nf, Y := range fr {
		for k := 0; k <= bkN/2; k++ {
			z[k] = Y[k]
			if k > 0 && k < bkN/2 {
				z[bkN-k] = cmplx.Conj(Y[k])
			}
		}
		fft(z, true) // the repo's inverse is unscaled or scaled; normalised below
		end := nf*bkH + bkH - 1
		for i := 0; i < bkLen; i++ {
			j := end - i
			if j < 0 {
				break
			}
			if j < len(y) {
				y[j] += b.p[i] * real(z[i%bkN])
			}
		}
	}
	return y[:n]
}

// reconstruction reports the bank's gain and delay on noise, and its error
// after both are removed.
func (b *bank) reconstruction() (gain float64, delay int, errDB float64) {
	rng := rand.New(rand.NewSource(1))
	x := make([]float64, 16000)
	for i := range x {
		x[i] = rng.NormFloat64()
	}
	y := b.synth(b.analyse(x), len(x))
	best := -1.0
	for d := -2 * bkLen; d <= 2*bkLen; d++ {
		var c, e float64
		for i := 4000; i < 12000; i++ {
			if j := i + d; j >= 0 && j < len(y) {
				c += x[i] * y[j]
				e += y[j] * y[j]
			}
		}
		if c > best {
			best, delay, gain = c, d, c/8000
		}
	}
	var num, den float64
	for i := 4000; i < 12000; i++ {
		j := i + delay
		if j >= 0 && j < len(y) {
			r := y[j] - gain*x[i]
			num += r * r
			den += x[i] * x[i] * gain * gain
		}
	}
	return gain, delay, 10 * math.Log10(num/den+1e-30)
}

// beamsInBank forms the eight beams from the mics' frames. taps true applies
// the table's four taps across frames; false folds them to one weight per band.
func beamsInBank(t *amazonTable, X [][][]complex128, taps bool) [][][]complex128 {
	nf := len(X[0])
	out := make([][][]complex128, amzBeams)
	for b := range out {
		out[b] = make([][]complex128, nf)
		for n := 0; n < nf; n++ {
			y := make([]complex128, bkN/2+1)
			for k := 1; k < amzBands; k++ {
				for m := 0; m < amzMics; m++ {
					if taps {
						for tp := 0; tp < amzTaps; tp++ {
							if n-tp >= 0 {
								y[k] += t.w(b, m, k, tp) * X[m][n-tp][k]
							}
						}
					} else {
						var w complex128
						for tp := 0; tp < amzTaps; tp++ {
							s := 1.0
							if (k*tp)%2 != 0 {
								s = -1
							}
							w += t.w(b, m, k, tp) * complex(s, 0)
						}
						y[k] += w * X[m][n][k]
					}
				}
			}
			out[b][n] = y
		}
	}
	return out
}

// planeWaveCheck sends a tone from each azimuth through the bank and beam b
// and reports where the beam peaks.
func planeWaveCheck(bk *bank, t *amazonTable, gain float64, delay int) {
	fmt.Println("\ntable in the bank, plane waves 1-2.5kHz: where each beam peaks (taps | fold)")
	for b := 0; b < amzBeams; b++ {
		var bestA [2]float64
		var bestG [2]float64
		for a := 0.0; a < 360; a += 5 {
			var g [2]float64
			for _, f := range []float64{1000, 1500, 2000, 2500} {
				mics := make([][]float64, amzMics)
				u := [2]float64{math.Sin(a * math.Pi / 180), math.Cos(a * math.Pi / 180)}
				for m, ch := range amazonChannels {
					p := positionOf(ch)
					adv := (u[0]*p[0] + u[1]*p[1]) / speedOfSound
					x := make([]float64, 8000)
					for i := range x {
						x[i] = math.Cos(2 * math.Pi * f * (float64(i)/sampleRate + adv))
					}
					mics[m] = x
				}
				X := make([][][]complex128, amzMics)
				for m := range X {
					X[m] = bk.analyse(mics[m])
				}
				for v, taps := range []bool{true, false} {
					B := beamsInBank(t, X, taps)
					y := bk.synth(B[b : b+1][0], 8000)
					var e float64
					for i := 4000; i < 7000; i++ {
						e += y[i] * y[i]
					}
					g[v] += math.Sqrt(2*e/3000) / gain
				}
			}
			for v := range g {
				if g[v] > bestG[v] {
					bestG[v], bestA[v] = g[v], a
				}
			}
		}
		fmt.Printf("  beam %d: taps %3.0f deg (%.2f)   fold %3.0f deg (%.2f)\n", b, bestA[0], bestG[0]/4, bestA[1], bestG[1]/4)
	}
}

func bankExperiment(rawPath, bankPath, tablePath string) {
	bk, err := loadBank(bankPath)
	if err != nil {
		fmt.Println("bank:", err)
		return
	}
	t, err := loadAmazon(tablePath)
	if err != nil {
		fmt.Println("table:", err)
		return
	}
	g, d, e := bk.reconstruction()
	fmt.Printf("bank: gain %.4f, delay %d samples, reconstruction error %.1f dB\n", g, d, e)
	lo, hi := bk.bandRolloff()
	fmt.Printf("bank alone: 200-400Hz %+.1fdB, 1.6-3.2kHz %+.1fdB (relative to the input)\n", lo, hi)
	planeWaveCheck(bk, t, g, d)

	bufs, err := readRaw(rawPath)
	if err != nil {
		fmt.Println(err)
		return
	}
	bf := beamformer.New()
	per := make([][]float64, beamformer.NumMics)
	for _, raw := range bufs {
		ch := bf.MicChannels(raw, 15.85, -1, nil)
		for m := range per {
			per[m] = append(per[m], s16Floats(ch[m])...)
		}
	}
	wins := speechWindows(per, rawHotDB)
	fmt.Printf("\nspeech found at (s): ")
	for _, w := range wins {
		fmt.Printf("%.1f-%.1f  ", w[0], w[1])
	}
	fmt.Println()
	n := len(per[0])
	X := make([][][]complex128, amzMics)
	for m, ch := range amazonChannels {
		X[m] = bk.analyse(per[ch])
	}
	type route struct {
		name   string
		groups [8][]float64
	}
	var routes []route
	groupOf := func(beams [][]float64) (gs [8][]float64) {
		for g := range gs {
			o := make([]float64, n)
			for k := 0; k < 3; k++ {
				for i, v := range beams[(g+k)%8] {
					o[i] += v / 3
				}
			}
			gs[g] = o
		}
		return
	}
	shift := func(y []float64) []float64 {
		o := make([]float64, n)
		for i := range o {
			if j := i + d; j >= 0 && j < len(y) {
				o[i] = y[j] / g
			}
		}
		return o
	}
	for _, c := range []struct {
		name string
		taps bool
	}{{"bank + taps (stock's way)", true}, {"bank + fold", false}} {
		B := beamsInBank(t, X, c.taps)
		beams := make([][]float64, 8)
		for b := range beams {
			beams[b] = shift(bk.synth(B[b], n))
		}
		routes = append(routes, route{c.name, groupOf(beams)})
	}
	// The firmware's own path, one group at a time, ARA off, plain group mean.
	table, err := beamformer.LoadStockTable(tablePath)
	if err != nil {
		fmt.Println("firmware table:", err)
		return
	}
	for _, c := range []struct {
		name string
		sd   *beamformer.SDWeights
	}{{"ours + table (ships now)", table}, {"ours + our design", nil}} {
		var r route
		r.name = c.name
		for grp := 0; grp < 8; grp++ {
			b := beamformer.New()
			b.SetWakeBeam(true)
			b.SetWakeBeamAra(false)
			var out []float64
			for _, raw := range bufs {
				mics := b.MicChannels(raw, 15.85, -1, nil)
				var o []byte
				for o = b.WakeBeam(mics); o == nil; o = b.WakeBeam(mics) {
				}
				if c.sd != nil {
					b.UseWakeBeamWeights(c.sd)
				}
				b.PinWakeGroup(grp)
				out = append(out, s16Floats(o)...)
			}
			r.groups[grp] = out
		}
		routes = append(routes, r)
	}
	fmt.Println("\neight groups per route, voice over background (400-2500Hz) / tilt, dB; best group marked")
	fmt.Printf("%-28s", "")
	for g := 0; g < 8; g++ {
		fmt.Printf("  grp%d       ", g)
	}
	fmt.Println(" best")
	for _, r := range routes {
		fmt.Printf("%-28s", r.name)
		bestV, bestG := -99.0, 0
		for g := 0; g < 8; g++ {
			v, bg := windowBands(r.groups[g], wins)
			var dd []float64
			for i := range v {
				x := math.Max(math.Pow(10, v[i]/10)-math.Pow(10, bg[i]/10), math.Pow(10, v[i]/10)*0.02)
				dd = append(dd, 10*math.Log10(x+1e-9))
			}
			vb, tilt := v[3]-bg[3], dd[4]-dd[1]
			fmt.Printf(" %+5.1f/%+5.1f", vb, tilt)
			if vb > bestV {
				bestV, bestG = vb, g
			}
		}
		fmt.Printf("  %d (%+.1f)\n", bestG, bestV)
	}
}

// bandRolloff is the bank's own analysis-synthesis gain in the tilt's two
// bands, dB, on noise: a droop here would show as tilt in every route through it.
func (b *bank) bandRolloff() (lo, hi float64) {
	rng := rand.New(rand.NewSource(2))
	x := make([]float64, 64000)
	for i := range x {
		x[i] = rng.NormFloat64()
	}
	y := b.synth(b.analyse(x), len(x))
	px, py := bandLevels(x[8000:56000]), bandLevels(y[8000:56000])
	return py[2] - px[2], py[4] - px[4] // 200-400Hz, 1.6-3.2kHz
}
