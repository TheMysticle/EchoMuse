package main

// Evaluates the beamformer coefficient table of a Radar firmware image against
// the same geometry and scenarios as the shipped design. The table is read
// from a path given on the command line; none of it is part of this repository.
//
// Layout, established by experiment on the file Radar's AFE.cfg loads
// ("ASR FixedBeamFormerV2": 8 beams, 4 mics, 4 taps): 32,768 values =
//
//	[beam 8][mic 4][band 128][tap 4][re, im]
//
// Every 1024-value block starts with eight zeros (band 0, DC), and read this
// way the eight beams have unity gain at their peak, a peak azimuth that holds
// within a degree across 1-2.5kHz, and eight distinct directions. The mic
// order is the AFE's channel map [0,1,3,4]. Bands are the 256-point filter
// bank's, 62.5Hz apart, with a hop of 128 samples, so tap t is a delay of t
// hops. The sign conventions (conjugate, hop phase, mic phase) cannot be told
// apart from magnitudes alone; all eight give the same set of beams up to a
// half turn, which an oracle beam choice does not see.

import (
	"bufio"
	"fmt"
	"math"
	"math/cmplx"
	"os"
	"regexp"
	"strconv"
)

const (
	amzBeams = 8
	amzMics  = 4
	amzBands = 128
	amzTaps  = 4
	amzHop   = 128.0
	amzBinHz = sampleRate / 256
)

// amazonChannels are the ALSA channels the wake path sums, in table order.
var amazonChannels = []int{0, 1, 3, 4}

type amazonTable struct{ v []float64 }

func loadAmazon(path string) (*amazonTable, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	re := regexp.MustCompile(`^\s*(-?\d+\.\d+),?\s*$`)
	var v []float64
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if m := re.FindStringSubmatch(sc.Text()); m != nil {
			x, err := strconv.ParseFloat(m[1], 64)
			if err != nil {
				return nil, err
			}
			v = append(v, x)
		}
	}
	if want := amzBeams * amzMics * amzBands * amzTaps * 2; len(v) != want {
		return nil, fmt.Errorf("%d values, want %d: not the 8-beam 4-mic table", len(v), want)
	}
	return &amazonTable{v}, nil
}

func (t *amazonTable) w(beam, mic, band, tap int) complex128 {
	i := ((beam*amzMics+mic)*amzBands+band)*amzTaps*2 + tap*2
	return complex(t.v[i], t.v[i+1])
}

// micFilter is mic m's frequency response in beam b at f: the sum over taps of
// weight times the hop delay of that tap.
func (t *amazonTable) micFilter(b, m int, f float64) complex128 {
	k := int(math.Round(f / amzBinHz))
	if k < 0 || k >= amzBands {
		return 0
	}
	var h complex128
	for tap := 0; tap < amzTaps; tap++ {
		h += t.w(b, m, k, tap) * cmplx.Exp(complex(0, -2*math.Pi*f*float64(tap)*amzHop/sampleRate))
	}
	return h
}

// response is beam b's gain for a plane wave of frequency f from (az, el).
func (t *amazonTable) response(b int, f, az, el float64) complex128 {
	u := unit(az, el)
	var y complex128
	for m, ch := range amazonChannels {
		p := positionOf(ch)
		adv := (u[0]*p[0] + u[1]*p[1]) / speedOfSound
		y += t.micFilter(b, m, f) * cmplx.Exp(complex(0, 2*math.Pi*f*adv))
	}
	return y
}

// A processor is anything with a set of beams and a response, so the shipped
// design and the table are measured by the same code.
type processor struct {
	name  string
	beams int
	resp  func(b int, f, az, el float64) complex128
	// micPower is the sum over mics of |filter|^2 at f, for independent noise.
	micPower func(b int, f float64) float64
}

func amazonProcessor(t *amazonTable) processor {
	return processor{
		name: "stock 8-beam (table)", beams: amzBeams, resp: t.response,
		micPower: func(b int, f float64) float64 {
			var s float64
			for m := 0; m < amzMics; m++ {
				a := cmplx.Abs(t.micFilter(b, m, f))
				s += a * a
			}
			return s
		},
	}
}

func shippedProcessor(set string) processor {
	pos := micPositions(set)
	d := newDesignSet(pos, 8, 4, 100, 5000)
	return processor{
		name: fmt.Sprintf("ours, %s (%s)", map[bool]string{false: "delay-and-sum", true: "superdirective"}[useSD], set), beams: d.n(),
		resp:     func(b int, f, az, el float64) complex128 { return d.resp(b, f, az, el) },
		micPower: func(b int, f float64) float64 { return d.noise(b, f) },
	}
}

func positionOf(ch int) [2]float64 { return micPositions("all")[ch] }

// peakAzimuth is the horizontal direction where beam b is strongest, averaged
// over 1-2.5kHz, to 1°.
func (p processor) peakAzimuth(b int) (az, gain float64) {
	best := -1.0
	for a := 0.0; a < 360; a += 1 {
		var g float64
		for _, f := range []float64{1000, 1500, 2000, 2500} {
			g += cmplx.Abs(p.resp(b, f, a, 0))
		}
		if g > best {
			best, az = g, a
		}
	}
	return az, best / 4
}

// di is the directivity index of beam b at f towards (az, 0).
func (p processor) di(b int, f, az float64) float64 {
	var sum, w float64
	const nAz, nEl = 72, 36
	for ie := 0; ie < nEl; ie++ {
		el := -90 + 180*(float64(ie)+0.5)/nEl
		cw := math.Cos(el * math.Pi / 180)
		for ia := 0; ia < nAz; ia++ {
			r := p.resp(b, f, 360*float64(ia)/nAz, el)
			sum += cw * (real(r)*real(r) + imag(r)*imag(r))
			w += cw
		}
	}
	l := p.resp(b, f, az, 0)
	return db((real(l)*real(l) + imag(l)*imag(l)) / (sum / w))
}

func (p processor) wng(b int, f, az float64) float64 {
	l := p.resp(b, f, az, 0)
	return db((real(l)*real(l) + imag(l)*imag(l)) / p.micPower(b, f))
}

// patternTable prints, per beam, where it points and its directivity there.
func patternTable(p processor) {
	fmt.Printf("\n%s: look direction and directivity per beam\n", p.name)
	fmt.Printf("%5s %8s %9s | DI dB at 250 500 1000 2000 3000 Hz | WNG dB at 250 500 1000 2000 Hz\n", "beam", "peak az", "peak gain")
	var avg [5]float64
	for b := 0; b < p.beams; b++ {
		az, g := p.peakAzimuth(b)
		fmt.Printf("%5d %7.0f° %9.2f | %7.2f %5.2f %5.2f %5.2f %5.2f | %8.2f %5.2f %5.2f %5.2f\n", b, az, g,
			p.di(b, 250, az), p.di(b, 500, az), p.di(b, 1000, az), p.di(b, 2000, az), p.di(b, 3000, az),
			p.wng(b, 250, az), p.wng(b, 500, az), p.wng(b, 1000, az), p.wng(b, 2000, az))
		for i, f := range []float64{250, 500, 1000, 2000, 3000} {
			avg[i] += p.di(b, f, az) / float64(p.beams)
		}
	}
	fmt.Printf("%5s %8s %9s | %7.2f %5.2f %5.2f %5.2f %5.2f |\n", "mean", "", "", avg[0], avg[1], avg[2], avg[3], avg[4])
}

// analyticSweep is snrReport without the time-domain simulation: for
// stationary sources the output power is a sum over bands of |response|^2
// times the source's spectrum, so the same scenarios can be run through a
// filter bank that has no time-domain form. Each target azimuth uses the
// processor's best beam for it (oracle), and the single mic is 1 everywhere.
func analyticSweep(procs []processor, inrDB, dnrDB, selfDB float64) {
	type band struct {
		name   string
		lo, hi float64
	}
	bs := []band{{"100-500", 100, 500}, {"500-1500", 500, 1500}, {"1500-4000", 1500, 4000}, {"100-4000", 100, 4000}}
	var freqs []float64
	for k := 2; k <= 64; k++ {
		freqs = append(freqs, float64(k)*amzBinHz)
	}
	// Spectra normalised to unit power over 100-4000Hz at one mic.
	norm := func(shape func(float64) float64) []float64 {
		s := make([]float64, len(freqs))
		var tot float64
		for i, f := range freqs {
			s[i] = shape(f) * shape(f)
			tot += s[i]
		}
		for i := range s {
			s[i] /= tot
		}
		return s
	}
	st, si, sd, sn := norm(speechShape), norm(musicShape), norm(speechShape), norm(flat)
	gi, gd, gn := math.Pow(10, inrDB/10), math.Pow(10, dnrDB/10), math.Pow(10, selfDB/10)
	sphere := fibonacciSphere(128)

	snr := func(p processor, b int, azT, azI float64, lo, hi float64) float64 {
		var t, n float64
		for i, f := range freqs {
			if f < lo || f > hi {
				continue
			}
			rt := cmplx.Abs(p.resp(b, f, azT, 0))
			ri := cmplx.Abs(p.resp(b, f, azI, 0))
			var diff float64
			for _, d := range sphere {
				r := cmplx.Abs(p.resp(b, f, d[0], d[1]))
				diff += r * r
			}
			diff /= float64(len(sphere))
			t += st[i] * rt * rt
			n += gi*si[i]*ri*ri + gd*sd[i]*diff + gn*sn[i]*p.micPower(b, f)
		}
		return db(t / n)
	}
	single := func(azT, azI, lo, hi float64) float64 {
		var t, n float64
		for i, f := range freqs {
			if f < lo || f > hi {
				continue
			}
			t += st[i]
			n += gi*si[i] + gd*sd[i] + gn*sn[i]
		}
		return db(t / n)
	}
	// Oracle beam: the one with the most target power over 500-4000Hz.
	best := func(p processor, azT float64) int {
		bb, bp := 0, -1.0
		for b := 0; b < p.beams; b++ {
			var s float64
			for _, f := range freqs {
				if f >= 500 && f <= 4000 {
					r := cmplx.Abs(p.resp(b, f, azT, 0))
					s += r * r
				}
			}
			if s > bp {
				bb, bp = b, s
			}
		}
		return bb
	}

	fmt.Printf("\noutput SNR (analytic), dB, mean over 16 target azimuths; interferer %+.0fdB, diffuse %+.0fdB, self-noise %+.0fdB re target at a mic\n", inrDB, dnrDB, selfDB)
	for _, off := range []float64{90, 180} {
		fmt.Printf("interferer +%.0f°%s", off, "")
		fmt.Printf("\n%-34s", "")
		for _, b := range bs {
			fmt.Printf(" %10s", b.name)
		}
		fmt.Println()
		row := func(name string, f func(azT, azI, lo, hi float64) float64) {
			fmt.Printf("%-34s", name)
			for _, b := range bs {
				var s float64
				for k := 0; k < 16; k++ {
					azT := 22.5 * float64(k)
					s += f(azT, azT+off, b.lo, b.hi)
				}
				fmt.Printf(" %10.2f", s/16)
			}
			fmt.Println()
		}
		row("single mic", single)
		for _, p := range procs {
			p := p
			row(p.name, func(azT, azI, lo, hi float64) float64 { return snr(p, best(p, azT), azT, azI, lo, hi) })
		}
	}
}

func amazonReport(path string, inr, dnr, self float64, set string) error {
	t, err := loadAmazon(path)
	if err != nil {
		return err
	}
	stock := amazonProcessor(t)
	ours := shippedProcessor(set)
	amz4 := shippedProcessor("amazon4")
	patternTable(stock)
	patternTable(amz4)
	procs := []processor{stock, amz4}
	if set != "amazon4" {
		procs = append(procs, ours)
	}
	analyticSweep(procs, inr, dnr, self)
	return nil
}
