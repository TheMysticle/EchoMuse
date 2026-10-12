// bf_design derives a filter-and-sum beam set from the board's documented mic
// geometry and measures it against the single-mic selection the firmware
// uses. Offline only: nothing here is imported by the firmware, and the
// firmware's choice of selection over summing stands (device/CLAUDE.md,
// "Beamformer"). This answers whether a beam of Amazon's stated size (8 beams,
// 4-tap filters) would have changed that, on this array.
//
//	go run ./tools/bf_design
//	go run ./tools/bf_design -mics amazon4 -dump
package main

import (
	"flag"
	"fmt"
	"math"
	"math/rand"
	"os"
	"strconv"
	"strings"

	"github.com/wilbowes/EchoMuse/internal/beamformer"
)

var bands = []struct {
	name   string
	lo, hi float64
}{
	{"100-500", 100, 500},
	{"500-1500", 500, 1500},
	{"1500-4000", 1500, 4000},
	{"100-4000", 100, 4000},
}

func main() {
	var (
		nBeams = flag.Int("beams", 8, "number of beams, evenly spaced from 0°")
		taps   = flag.Int("taps", 4, "taps per mic fractional-delay interpolator")
		mics   = flag.String("mics", "all", "mic set: all | perimeter | amazon4")
		inr    = flag.Float64("inr", 6, "interferer power at a mic relative to the target, dB")
		dnr    = flag.Float64("dnr", -6, "diffuse noise power at a mic relative to the target, dB")
		snr    = flag.Float64("self", -30, "mic self-noise power relative to the target, dB")
		seed   = flag.Int64("seed", 1, "random seed")
		dump   = flag.Bool("dump", false, "print the derived taps")
		nfft   = flag.Int("n", 1<<15, "record length in samples (power of two)")
		amazon = flag.String("amazon", "", "path to a Radar AFE beamformer coefficient table to measure beside the shipped design")
	)
	flag.BoolVar(&useSD, "sd", false, "superdirective design instead of delay-and-sum")
	flag.Float64Var(&sdWNG, "wng", sdWNG, "shift of the white-noise-gain floor profile for -sd, dB (0 = stock's measured profile)")
	flag.Float64Var(&beamformer.SDRingWeight, "ring", beamformer.SDRingWeight, "directional-interference ring power re diffuse for -sd (0 = diffuse only)")
	az := flag.String("azimuths", "", "look directions for -sd, degrees, comma separated (default: even spacing)")
	flag.Float64Var(&beamformer.SDRingLowHz, "ring.low", beamformer.SDRingLowHz, "taper the interference ring to zero below this frequency, Hz")
	flag.Float64Var(&beamformer.SDSectorDeg, "sector", beamformer.SDSectorDeg, "half-width of the look sector the ring leaves alone, degrees")
	selOn := flag.Bool("sel", false, "drive the real wake path through a living-room scene with a quiet distant talker")
	selFlags()
	epClip := flag.String("ep.clip", "", "run the endpointer on a recorded session (comma separated WAV paths); the TV-only part of the recording, repeated to -ep.history seconds, stands in for the room before the wake")
	rawPath := flag.String("raw", "", "a raw 9-channel S24_3LE capture (the firmware's capture.req, or capture_mics): report each mic and each wake-path mode by band")
	rawFrom := flag.Float64("raw.from", 0, "start of the window in the capture, seconds")
	flag.Float64Var(&rawHotDB, "raw.hot", 8, "dB over the median for a 250ms block to count as speech in -raw")
	rawTo := flag.Float64("raw.to", 0, "end of the window, seconds (0 = the end)")
	epHist := flag.Float64("ep.history", 11, "seconds of the recording's tail used as the room before the wake word")
	selTonal := flag.Bool("sel.tonal", false, "with -sel: measure in one run, talker and TV as interleaved tone combs (use for the canceller)")
	colorOn := flag.Bool("color", false, "how much a talker's spectrum is coloured by the beams, by how far off-axis they are")
	araOn := flag.Bool("ara", false, "measure the interference canceller after the beams (use with -sd)")
	araFlags()
	flag.Parse()
	for _, f := range strings.Split(*az, ",") {
		if v, err := strconv.ParseFloat(strings.TrimSpace(f), 64); err == nil {
			beamformer.SDAzimuths = append(beamformer.SDAzimuths, v)
		}
	}

	if *amazon != "" {
		if err := amazonReport(*amazon, *inr, *dnr, *snr, *mics); err != nil {
			fmt.Fprintln(os.Stderr, "bf_design:", err)
			os.Exit(1)
		}
		return
	}
	pos := micPositions(*mics)
	beams := newDesignSet(pos, *nBeams, *taps, 100, 5000)
	if *rawPath != "" {
		rawReport(*rawPath, *rawFrom, *rawTo)
		return
	}
	if *epClip != "" {
		for _, p := range strings.Split(*epClip, ",") {
			epClipReport(p, *epHist)
		}
		return
	}
	if *selOn && *selTonal {
		selTonalReport(rand.New(rand.NewSource(*seed)))
		return
	}
	if *selOn {
		selReport(rand.New(rand.NewSource(*seed)))
		return
	}
	if *colorOn {
		colorReport(beams)
		return
	}
	if *araOn {
		araReport(rand.New(rand.NewSource(*seed)), pos, beams, *inr, *dnr, *snr)
		return
	}
	fmt.Printf("array: %d mics (%s), 36mm radius; %s\n", len(pos), *mics, beams.describe())

	if *dump && beams.das != nil {
		dumpTaps(beams.das[0])
	}
	patternReport(beams, 0)

	rng := rand.New(rand.NewSource(*seed))
	snrReport(rng, pos, beams, *nfft, *inr, *dnr, *snr)
}

func dumpTaps(b Beam) {
	fmt.Printf("\nbeam %.0f° taps (rows: mic; cols: sample index; weights include 1/M):\n", b.AzimuthDeg)
	for i, h := range b.H {
		fmt.Printf("  mic %d (delay %.3f):", i, b.Delay[i])
		for _, c := range h {
			fmt.Printf(" %8.4f", c)
		}
		fmt.Println()
	}
}

// patternReport prints beam b's response at fixed frequencies: look-direction
// error, the level at offsets from its look direction, directivity index and
// white-noise gain. Every number is for the beams as they ship.
func patternReport(d *designSet, b int) {
	fmt.Printf("\nbeam %d (look %.0f°) response, dB re look direction, at offsets from the look direction, horizontal plane\n", b, d.az(b))
	offs := []float64{45, 90, 135, 180, 225, 270, 315}
	fmt.Printf("%7s %9s", "Hz", "lookErr")
	for _, o := range offs {
		fmt.Printf(" %+6.0f°", o)
	}
	fmt.Printf(" %7s %7s %7s\n", "DI dB", "WNG dB", "worst")
	for _, f := range []float64{250, 500, 1000, 1500, 2000, 3000, 4000} {
		look := d.look(b, f)
		fmt.Printf("%7.0f %+8.2fdB", f, 20*math.Log10(absC(look)))
		worst := math.Inf(-1)
		for _, o := range offs {
			r := 20 * math.Log10(absC(d.resp(b, f, d.az(b)+o, 0))/absC(d.resp(b, f, d.az(b), 0)))
			fmt.Printf(" %+6.1f", r)
			worst = math.Max(worst, r)
		}
		fmt.Printf(" %7.2f %7.2f %+7.1f\n", directivityIndex(d, b, f), whiteNoiseGain(d, b, f), worst)
	}
}

func absC(c complex128) float64 { return math.Hypot(real(c), imag(c)) }

// directivityIndex is the look-direction power over the power averaged across
// the whole sphere, in dB. 0dB is an omni mic; a sum of M uncorrelated
// inputs could reach 10log10(M) only at high frequency, where the field
// decorrelates.
func directivityIndex(d *designSet, b int, f float64) float64 {
	var sum, w float64
	const nAz, nEl = 72, 36
	for ie := 0; ie < nEl; ie++ {
		el := -90 + 180*(float64(ie)+0.5)/nEl
		cw := math.Cos(el * math.Pi / 180)
		for ia := 0; ia < nAz; ia++ {
			r := d.resp(b, f, 360*float64(ia)/nAz, el)
			sum += cw * (real(r)*real(r) + imag(r)*imag(r))
			w += cw
		}
	}
	l := d.resp(b, f, d.az(b), 0)
	return db((real(l)*real(l) + imag(l)*imag(l)) / (sum / w))
}

// whiteNoiseGain is the SNR change for noise independent between mics (self
// noise): look-direction power over the summed squared per-mic gains.
// 10log10(M) for plain delay-and-sum; below 0dB it is amplifying self-noise.
func whiteNoiseGain(d *designSet, b int, f float64) float64 {
	l := d.resp(b, f, d.az(b), 0)
	return db((real(l)*real(l) + imag(l)*imag(l)) / d.noise(b, f))
}

// snrReport sweeps target azimuth over 16 directions (22.5° steps, so half
// the cases fall midway between two beams, the worst case for a beam set),
// with an interferer at +90° and +180° from the target. For each, the output
// SNR (target vs interferer + diffuse + self-noise) of three processors:
// the centre mic (what the wake word listens on), the perimeter mic nearest
// the target (the best the onset-ratio selector could do), and the nearest beam.
func snrReport(rng *rand.Rand, pos [][2]float64, beams *designSet, n int, inrDB, dnrDB, selfDB float64) {
	const lo, hi = 100.0, 4000.0
	diffuse := scale(Diffuse(rng, pos, n, 64, lo, hi), math.Pow(10, dnrDB/20))
	self := scale(SelfNoise(rng, len(pos), n, lo, hi), math.Pow(10, selfDB/20))
	interfAmp := math.Pow(10, inrDB/20)

	centre := -1
	for i, p := range pos {
		if p[0] == 0 && p[1] == 0 {
			centre = i
		}
	}

	type acc struct{ sum [4]float64 }
	results := map[string]*acc{}
	order := []string{}
	add := func(key string, band int, v float64) {
		a, ok := results[key]
		if !ok {
			a = &acc{}
			results[key] = a
			order = append(order, key)
		}
		a.sum[band] += v
	}
	counts := map[string]int{}

	for _, off := range []float64{90, 180} {
		for step := 0; step < 16; step++ {
			az := 22.5 * float64(step)
			tgt := NewSource(rng, n, lo, hi, speechShape).AtMics(pos, az, 0)
			intf := scale(NewSource(rng, n, lo, hi, musicShape).AtMics(pos, az+off, 0), interfAmp)
			noise := sumSignals(intf, diffuse, self)

			bi := nearestBeam(beams, az)
			near := nearestPerimeter(pos, centre, az)
			type proc struct {
				name     string
				tgt, nse []float64
			}
			var procs []proc
			if centre >= 0 {
				procs = append(procs, proc{"centre mic", tgt[centre], sumRow(noise, centre)})
			}
			procs = append(procs,
				proc{"nearest perimeter mic", tgt[near], sumRow(noise, near)},
				proc{fmt.Sprintf("%d-beam %s", beams.n(), map[bool]string{false: "filter-and-sum", true: "superdirective"}[beams.sd != nil]), beams.apply(tgt, bi), beams.apply(noise, bi)})
			key := fmt.Sprintf("interferer +%.0f°", off)
			for _, pr := range procs {
				for bi, bd := range bands {
					s := bandPower(pr.tgt, bd.lo, bd.hi)
					v := bandPower(pr.nse, bd.lo, bd.hi)
					add(key+"|"+pr.name, bi, db(s/v))
				}
				counts[key+"|"+pr.name]++
			}
		}
	}

	fmt.Printf("\noutput SNR, dB (mean over 16 target azimuths; interferer %+.0fdB, diffuse %+.0fdB, self-noise %+.0fdB re target at a mic)\n", inrDB, dnrDB, selfDB)
	fmt.Printf("%-44s", "")
	for _, bd := range bands {
		fmt.Printf(" %10s", bd.name)
	}
	fmt.Println()
	base := map[string]*acc{}
	for _, key := range order {
		a := results[key]
		c := float64(counts[key])
		fmt.Printf("%-44s", key)
		for b := range bands {
			fmt.Printf(" %10.2f", a.sum[b]/c)
		}
		fmt.Println()
		scenario, proc := splitKey(key)
		if proc == "nearest perimeter mic" {
			base[scenario] = a
		}
	}
	fmt.Printf("\nbeam improvement over the nearest perimeter mic, dB (positive = beam is better)\n")
	fmt.Printf("%-44s", "")
	for _, bd := range bands {
		fmt.Printf(" %10s", bd.name)
	}
	fmt.Println()
	for _, key := range order {
		scenario, proc := splitKey(key)
		if proc == "nearest perimeter mic" || proc == "centre mic" {
			continue
		}
		a, b := results[key], base[scenario]
		c := float64(counts[key])
		fmt.Printf("%-44s", scenario)
		for i := range bands {
			fmt.Printf(" %+10.2f", (a.sum[i]-b.sum[i])/c)
		}
		fmt.Println()
	}
}

func splitKey(k string) (scenario, proc string) {
	for i := range k {
		if k[i] == '|' {
			return k[:i], k[i+1:]
		}
	}
	return k, ""
}

func sumSignals(a ...[][]float64) [][]float64 {
	out := make([][]float64, len(a[0]))
	for i := range out {
		out[i] = make([]float64, len(a[0][i]))
		for _, s := range a {
			for t, v := range s[i] {
				out[i][t] += v
			}
		}
	}
	return out
}

// sumRow wraps one row so it can be treated like a beam output.
func sumRow(x [][]float64, i int) []float64 { return x[i] }

func nearestBeam(beams *designSet, az float64) int {
	best, bd := 0, math.Inf(1)
	for i := 0; i < beams.n(); i++ {
		if d := angDiff(az, beams.az(i)); d < bd {
			best, bd = i, d
		}
	}
	return best
}

// nearestPerimeter picks the perimeter mic closest in azimuth to the target,
// which is the best case for the onset-ratio selector.
func nearestPerimeter(pos [][2]float64, centre int, az float64) int {
	best, bd := 0, math.Inf(1)
	for i, p := range pos {
		if i == centre {
			continue
		}
		a := math.Atan2(p[0], p[1]) * 180 / math.Pi
		if d := angDiff(az, a); d < bd {
			best, bd = i, d
		}
	}
	return best
}

func angDiff(a, b float64) float64 {
	d := math.Mod(math.Abs(a-b), 360)
	if d > 180 {
		d = 360 - d
	}
	return d
}

// flagFloat registers a float flag bound to a package variable, defaulting to
// its current value.
func flagFloat(p *float64, name, usage string) { flag.Float64Var(p, name, *p, usage) }

// colorReport measures what a recogniser would hear of a talker's spectrum. A
// beam passes its look direction at exactly unity, but a talker is never exactly
// there, and off the look direction the response is not flat: it tilts and
// ripples with frequency. For each offset from a beam's look direction this
// prints the response over 190-4000Hz (the bins that carry speech): its mean
// level, the standard deviation of its shape in dB (how coloured), and its
// spread. A single mic is 0 for all of them. The offset a talker actually sees
// is the angle to the nearest beam, which the last rows give for these beams.
func colorReport(d *designSet) {
	fmt.Printf("\ncolouring of a talker's spectrum by the beams, 190-4000Hz, mean over the %d beams\n", d.n())
	fmt.Printf("%12s %12s %14s %18s\n", "off look dir", "mean level", "shape std dev", "lowest..highest")
	for _, off := range []float64{0, 5, 10, 15, 20, 25, 30, 45} {
		var lvl, sd, lo, hi float64
		cnt := 0
		for b := 0; b < d.n(); b++ {
			for _, sign := range []float64{1, -1} {
				var v []float64
				for k := 3; k <= 64; k++ {
					f := float64(k) * sampleRate / 256
					v = append(v, 20*math.Log10(math.Max(absC(d.resp(b, f, d.az(b)+sign*off, 0)), 1e-6)))
				}
				var m float64
				mn, mx := math.Inf(1), math.Inf(-1)
				for _, x := range v {
					m += x
					mn, mx = math.Min(mn, x), math.Max(mx, x)
				}
				m /= float64(len(v))
				var ss float64
				for _, x := range v {
					ss += (x - m) * (x - m)
				}
				lvl += m
				sd += math.Sqrt(ss / float64(len(v)))
				lo += mn - m
				hi += mx - m
				cnt++
			}
		}
		c := float64(cnt)
		fmt.Printf("%11.0f° %10.1fdB %12.1fdB %9.1f..%+.1fdB\n", off, lvl/c, sd/c, lo/c, hi/c)
	}
	// The offset a talker actually has: the angle to the nearest beam, for a
	// talker at every degree.
	var worst, sum float64
	for az := 0.0; az < 360; az++ {
		best := math.Inf(1)
		for b := 0; b < d.n(); b++ {
			best = math.Min(best, angDiff(az, d.az(b)))
		}
		worst = math.Max(worst, best)
		sum += best
	}
	fmt.Printf("a talker is at most %.0f° (mean %.1f°) from the nearest beam's look direction\n", worst, sum/360)
}
