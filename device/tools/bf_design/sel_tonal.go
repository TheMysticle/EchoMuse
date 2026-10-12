package main

// Single-run measurement of the wake path against a TV.
//
// selReport measures the talker as the output with him minus the output without,
// which is only valid while the path does the same thing in both runs. The
// canceller adapts to whatever it hears, so with a TV that loud it does not, and
// that measurement fails (it reads -13dB where a tone test shows a few dB of
// loss).
//
// Here the talker and the TV are combs of tones in the SAME 62.5Hz canceller
// bins, 28Hz apart, so the canceller cannot tell them apart by bin and has to
// treat them as it treats speech and music. One pass through the path then
// measures each at its own frequencies, with nothing subtracted. The tones are
// far more predictable than music, so the canceller does better here than it
// will on a real TV: this bounds what it can do against the talker rather than
// what it will do for the user.

import (
	"fmt"
	"math"
	"math/rand"
)

// comb is a source made of tones at hz, as a Source so it can be delayed
// exactly per microphone.
func comb(rng *rand.Rand, n int, hz []float64) Source {
	return NewSource(rng, n, 100, 4000, func(f float64) float64 {
		for _, t := range hz {
			if math.Abs(f-t) < 1.2 {
				return 1
			}
		}
		return 0
	})
}

// combPower is the power of x[lo:hi] within +-width of each of hz, by a Hann
// windowed transform.
func combPower(x []float64, lo, hi int, hz []float64, width float64) float64 {
	const nfft = 16384
	seg := x[lo:hi]
	if len(seg) > nfft {
		seg = seg[:nfft]
	}
	buf := make([]complex128, nfft)
	var wsum float64
	for i, v := range seg {
		w := 0.5 - 0.5*math.Cos(2*math.Pi*float64(i)/float64(len(seg)-1))
		buf[i] = complex(v*w, 0)
		wsum += w * w
	}
	fft(buf, false)
	var p float64
	for k := 0; k <= nfft/2; k++ {
		f := float64(k) * sampleRate / nfft
		for _, t := range hz {
			if math.Abs(f-t) <= width {
				p += real(buf[k])*real(buf[k]) + imag(buf[k])*imag(buf[k])
				break
			}
		}
	}
	return p / (wsum * float64(len(seg)))
}

func selTonalReport(rng *rand.Rand) {
	const (
		n        = 1 << 20
		burstLen = 19200
		period   = 1 << 16
	)
	var talkHz, tvHz []float64
	for k := 7; k <= 56; k++ { // 437-3500Hz, a tone either side of each bin's centre
		talkHz = append(talkHz, 62.5*float64(k)-14)
		tvHz = append(tvHz, 62.5*float64(k)+14)
	}
	pos := micPositions("all")
	noise := sumSignals(
		scale(Diffuse(rng, pos, n, 24, 100, 4000), selDiffuse),
		scale(SelfNoise(rng, len(pos), n, 100, 4000), selSelf))
	tvOn := selTV > 0
	if tvOn {
		m := scale(comb(rng, period, tvHz).AtMics(pos, selTVAz, 0), selTV)
		for ch := range noise {
			for i := 0; i < n; i++ {
				noise[ch][i] += m[ch][i%period]
			}
		}
	}
	type burst struct {
		start int
		az    float64
	}
	var bursts []burst
	speech := make([][]float64, len(pos))
	for m := range speech {
		speech[m] = make([]float64, n)
	}
	for k := 0; k < selBursts; k++ {
		start := 12*16000 + k*7*16000 + rng.Intn(16000)
		if start+burstLen >= n {
			break
		}
		az := selAz
		if az < 0 {
			az = rng.Float64() * 360
		}
		bursts = append(bursts, burst{start, az})
		mics := scale(comb(rng, period, talkHz).AtMics(pos, az, 0), selTalker)
		for m := range speech {
			for i := 0; i < burstLen; i++ {
				t := float64(i) / sampleRate
				env := 0.15 + 0.85*math.Abs(math.Sin(2*math.Pi*3.5*t))
				fade := math.Min(1, math.Min(float64(i), float64(burstLen-i))/800)
				speech[m][start+i] += mics[m][i%period] * env * fade / 0.65
			}
		}
	}
	both := sumSignals(noise, speech)

	paths := []struct {
		name string
		run  func() []float64
	}{
		{"centre mic (the single-mic path)", func() []float64 { return both[6] }},
		{"wake beam", func() []float64 { o, _ := runWakePath(both, false, nil); return o }},
		{"wake beam + ARA", func() []float64 { o, _ := runWakePath(both, true, nil); return o }},
	}
	type res struct{ talker, tv []float64 }
	var rs []res
	for _, p := range paths {
		out := p.run()
		var r res
		for _, bu := range bursts {
			lo, hi := bu.start+4000, bu.start+burstLen
			r.talker = append(r.talker, db(combPower(out, lo, hi, talkHz, 7)))
			if tvOn {
				r.tv = append(r.tv, db(combPower(out, lo, hi, tvHz, 7)))
			}
		}
		rs = append(rs, r)
	}
	mean := func(v []float64) float64 {
		var s float64
		for _, x := range v {
			s += x
		}
		return s / float64(len(v))
	}
	fmt.Printf("\ntones: talker %.0f rms at a mic over %d bursts of 1.2s; TV %.0f rms at %.0f deg (0 = none); diffuse %.0f, self-noise %.0f\n",
		selTalker, len(bursts), selTV, selTVAz, selDiffuse, selSelf)
	fmt.Printf("%-34s %14s %14s %12s %14s\n", "", "talker level", "TV level", "SNR gain", "worst burst")
	for i, p := range paths {
		tal := mean(rs[i].talker) - mean(rs[0].talker)
		if !tvOn {
			fmt.Printf("%-34s %+11.1f dB\n", p.name, tal)
			continue
		}
		tv := mean(rs[i].tv) - mean(rs[0].tv)
		worst := math.Inf(1)
		for b := range rs[i].talker {
			g := (rs[i].talker[b] - rs[0].talker[b]) - (rs[i].tv[b] - rs[0].tv[b])
			worst = math.Min(worst, g)
		}
		fmt.Printf("%-34s %+11.1f dB %+11.1f dB %+9.1f dB %+11.1f dB\n", p.name, tal, tv, tal-tv, worst)
	}
}
