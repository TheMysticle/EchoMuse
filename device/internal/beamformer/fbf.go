package beamformer

// Filter-and-sum wake beam (`wakeBeam`, off by default).
//
// The default wake path reads one microphone (see Process). With wakeBeam on,
// the unlocked path instead sums all seven through one of eight fixed beams,
// chosen by the same onset-ratio statistic that picks a perimeter mic at
// Lock(), and falls back to the centre mic when nothing stands out. Locked
// turns are untouched.
//
// This is delay-and-sum, and device/CLAUDE.md records why the turn path is not:
// at a 72mm aperture a sum has almost nothing to cancel below 1.5kHz
// (tools/bf_design measures +0.2 to +2.6dB there). The gain is above that,
// where it is roughly 10dB against a single interferer. It is an experiment
// to A/B on a device, which is why it is a setting and not a replacement.

import (
	"log"
	"math"
)

const (
	// fbfBeams are spaced evenly from 0°, clockwise from 12 o'clock.
	fbfBeams = 8
	// fbfTaps is the length of each mic's fractional-delay interpolator.
	fbfTaps = 4
	// fbfBandLo/Hi is the band the interpolators are fitted over.
	fbfBandLo, fbfBandHi = 100.0, 5000.0

	// fbfBlock is the selection granularity: 8ms, the AFE's own frame.
	fbfBlock = 128
	// fbfWarmBlocks is how long selection stays on the centre mic after the
	// stream starts, so the slow baseline has something to be a baseline of
	// (3s, as the perimeter selector's warm-up).
	fbfWarmBlocks = 375
	// fbfHoldBlocks is the minimum time on a beam before another may take
	// over: 96ms, the stock group merger's hold.
	fbfHoldBlocks = 12
	// fbfReleaseBlocks is how long the chosen beam may sit under
	// fbfReleaseRatio before the path goes back to the centre mic (1s).
	fbfReleaseBlocks = 125
	// fbfEngageRatio is the onset ratio (fast/slow energy) a beam needs
	// before it replaces the centre mic, and fbfSwitchMargin how much better
	// than the current beam another must be to take over.
	fbfEngageRatio  = 2.0
	fbfReleaseRatio = 1.3
	fbfSwitchMargin = 1.25
	// fbfFade is the crossfade, in samples, when the output changes beam.
	fbfFade = 32

	// Smoothing, rescaled from the perimeter selector's per-32ms constants
	// (0.9 and 0.995) to per-block: ~300ms fast, ~10s slow.
	fbfFastAlpha = 0.9738
	fbfSlowAlpha = 0.99875

	// perimeterRadiusM is the perimeter mics' distance from the centre.
	perimeterRadiusM = 0.036
	speedOfSound     = 343.0

	// fbfNeutral is the output index of the centre mic, delayed to match.
	fbfNeutral = fbfBeams
)

// MicPosition returns ALSA channel ch's position in metres: x to the right,
// y towards 12 o'clock, centre mic at the origin. Derived from micAngles, so
// the geometry exists once.
func MicPosition(ch int) [2]float64 {
	if ch == centreCh {
		return [2]float64{}
	}
	a := micAngles[ch] * math.Pi / 180
	return [2]float64{perimeterRadiusM * math.Sin(a), perimeterRadiusM * math.Cos(a)}
}

// Beam is one filter-and-sum beam: an FIR per microphone, summed, with the
// 1/M weight folded in.
type Beam struct {
	AzimuthDeg float64
	// H[mic] is that mic's FIR, indexed by delay in samples.
	H [][]float64
	// Delay[mic] is the delay that FIR was designed for, in samples.
	Delay []float64
	// Bulk is the delay every mic's path has at the look direction. It is the
	// same for every beam from one Design call, so changing beam changes the
	// direction and not the latency.
	Bulk float64
}

// DesignBeams derives nBeams beams, evenly spaced from 0°, for mics at pos.
//
// Each mic is delayed by its projection on the look direction over the speed
// of sound (a plane wave from azimuth a reaches mic i (u·p_i)/c before it
// reaches the origin), plus one offset common to ALL beams: the whole
// numbers of samples that make the most-advanced mic in any beam causal. The
// offset is an integer so a mic at the origin is delayed by exactly that many
// samples, which is what lets the centre mic stand in for a beam with no
// direction (see fbfNeutral).
//
// A delay is not a whole number of samples at 16kHz (the 72mm aperture spans
// 3.4), so each is an integer shift plus a taps-long least-squares
// fractional-delay FIR fitted over [fLo, fHi].
func DesignBeams(pos [][2]float64, nBeams, taps int, fLo, fHi float64) []Beam {
	lead := taps/2 - 1
	proj := make([][]float64, nBeams)
	worst := 0.0
	for b := range proj {
		u := unitVec(360*float64(b)/float64(nBeams), 0)
		proj[b] = make([]float64, len(pos))
		for i, p := range pos {
			proj[b][i] = (u[0]*p[0] + u[1]*p[1]) / speedOfSound * sampleRate
			worst = math.Max(worst, -proj[b][i])
		}
	}
	off := float64(lead) + math.Ceil(worst)
	w := 1.0 / float64(len(pos))
	beams := make([]Beam, nBeams)
	for b := range beams {
		bm := Beam{AzimuthDeg: 360 * float64(b) / float64(nBeams), Bulk: off,
			Delay: make([]float64, len(pos)), H: make([][]float64, len(pos))}
		start := make([]int, len(pos))
		hs := make([][]float64, len(pos))
		length := 0
		for i := range pos {
			d := proj[b][i] + off
			bm.Delay[i] = d
			n := math.Floor(d)
			hs[i] = fractionalDelayLS(taps, float64(lead)+d-n, fLo, fHi)
			start[i] = int(n) - lead
			length = max(length, start[i]+taps)
		}
		for i := range pos {
			bm.H[i] = make([]float64, length)
			for k, v := range hs[i] {
				bm.H[i][start[i]+k] = v * w
			}
		}
		beams[b] = bm
	}
	return beams
}

func unitVec(azDeg, elDeg float64) [3]float64 {
	a, e := azDeg*math.Pi/180, elDeg*math.Pi/180
	return [3]float64{math.Sin(a) * math.Cos(e), math.Cos(a) * math.Cos(e), math.Sin(e)}
}

// fractionalDelayLS returns taps coefficients whose response is the
// least-squares fit of a pure delay of delay samples over [fLo, fHi]. For real
// h the objective sum_w |sum_k h_k e^{-jwk} - e^{-jw delay}|^2 reduces to
// A h = b with A_kl = sum cos(w(k-l)) and b_k = sum cos(w(k-delay)).
func fractionalDelayLS(taps int, delay, fLo, fHi float64) []float64 {
	const grid = 400
	a := make([][]float64, taps)
	for i := range a {
		a[i] = make([]float64, taps+1)
	}
	for g := 0; g < grid; g++ {
		w := 2 * math.Pi * (fLo + (fHi-fLo)*(float64(g)+0.5)/grid) / sampleRate
		for k := 0; k < taps; k++ {
			for l := 0; l < taps; l++ {
				a[k][l] += math.Cos(w * float64(k-l))
			}
			a[k][taps] += math.Cos(w * (float64(k) - delay))
		}
	}
	return gaussJordan(a)
}

// gaussJordan solves an n x (n+1) system with partial pivoting.
func gaussJordan(a [][]float64) []float64 {
	n := len(a)
	for c := 0; c < n; c++ {
		p := c
		for r := c + 1; r < n; r++ {
			if math.Abs(a[r][c]) > math.Abs(a[p][c]) {
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
	x := make([]float64, n)
	for i := range x {
		x[i] = a[i][n] / a[i][i]
	}
	return x
}

// fbfTap is one nonzero coefficient of one beam.
type fbfTap struct {
	mic, delay int
	c          float32
}

// wakeBeam is the filter-and-sum state: the filter set, each mic's recent
// past, and the selection statistics.
type wakeBeam struct {
	beams []Beam
	taps  [fbfBeams][]fbfTap
	// off is the delay of the neutral (centre mic) output.
	off int
	// hist holds each mic's last maxDelay samples; ext is the scratch the
	// current period is appended to.
	hist     [NumMics][]float32
	ext      [NumMics][]float32
	maxDelay int
	// out[k] is output k's signal for the period: beams 0-7 and the neutral.
	out [fbfBeams + 1][]float32

	// Per-output HF energy, fast and slow, and the carry for the band-diff.
	fast, slow [fbfBeams + 1]float64
	prev2      [fbfBeams + 1][2]float32
	blocks     int // blocks seen, for the warm-up

	cur      int // output in use, fbfNeutral or a beam
	holdLeft int // blocks before cur may change
	lowFor   int // consecutive blocks cur has sat under fbfReleaseRatio
	switches uint64
}

func newWakeBeam() *wakeBeam {
	pos := make([][2]float64, NumMics)
	for ch := range pos {
		pos[ch] = MicPosition(ch)
	}
	w := &wakeBeam{beams: DesignBeams(pos, fbfBeams, fbfTaps, fbfBandLo, fbfBandHi), cur: fbfNeutral}
	w.off = int(w.beams[0].Bulk)
	for b, bm := range w.beams {
		for mic, h := range bm.H {
			for d, c := range h {
				if c != 0 {
					w.taps[b] = append(w.taps[b], fbfTap{mic, d, float32(c)})
					w.maxDelay = max(w.maxDelay, d)
				}
			}
		}
	}
	w.maxDelay = max(w.maxDelay, w.off)
	for i := range w.hist {
		w.hist[i] = make([]float32, w.maxDelay)
	}
	return w
}

// process runs one period of NumMics S16 buffers through every beam, picks the
// output per block and returns it as S16. locked freezes the statistics and
// parks the selection on the centre mic: a turn's own voice must not move the
// baseline, and the path should start the next listen from neutral.
func (w *wakeBeam) process(mics [][]byte, gain float64, locked bool) (out []byte, clipped uint64) {
	n := len(mics[0]) / 2
	for m := 0; m < NumMics; m++ {
		if cap(w.ext[m]) < w.maxDelay+n {
			w.ext[m] = make([]float32, w.maxDelay+n)
		}
		e := w.ext[m][:w.maxDelay+n]
		copy(e, w.hist[m])
		for i := 0; i < n; i++ {
			e[w.maxDelay+i] = float32(int16(uint16(mics[m][i*2]) | uint16(mics[m][i*2+1])<<8))
		}
		copy(w.hist[m], e[n:])
		w.ext[m] = e
	}
	for k := range w.out {
		if cap(w.out[k]) < n {
			w.out[k] = make([]float32, n)
		}
		w.out[k] = w.out[k][:n]
	}
	for b := 0; b < fbfBeams; b++ {
		y := w.out[b]
		clear(y)
		for _, t := range w.taps[b] {
			x := w.ext[t.mic][w.maxDelay-t.delay:]
			for i := range y {
				y[i] += t.c * x[i]
			}
		}
	}
	copy(w.out[fbfNeutral], w.ext[centreCh][w.maxDelay-w.off:])

	out = make([]byte, n*2)
	g := float32(gain)
	prevSel := w.cur
	for s := 0; s < n; s += fbfBlock {
		e := min(s+fbfBlock, n)
		if locked {
			w.cur, w.holdLeft, w.lowFor = fbfNeutral, 0, 0
		} else {
			w.step(s, e)
		}
		for i := s; i < e; i++ {
			v := w.out[w.cur][i]
			if w.cur != prevSel && i-s < fbfFade {
				f := float32(i-s+1) / fbfFade
				v = v*f + w.out[prevSel][i]*(1-f)
			}
			v *= g
			var q int32
			switch {
			case v >= 32767:
				q, clipped = 32767, clipped+1
			case v <= -32768:
				q, clipped = -32768, clipped+1
			default:
				q = int32(math.Round(float64(v)))
			}
			out[i*2], out[i*2+1] = byte(uint16(q)), byte(uint16(q)>>8)
		}
		prevSel = w.cur
	}
	return out, clipped
}

// step folds block [s,e) into the statistics and moves the selection.
func (w *wakeBeam) step(s, e int) {
	var ratio [fbfBeams + 1]float64
	for k := range w.out {
		var en float64
		x := w.out[k]
		p0, p1 := w.prev2[k][0], w.prev2[k][1] // x[s-2], x[s-1]
		for i := s; i < e; i++ {
			d := (x[i] - p0) * 0.5
			en += float64(d) * float64(d)
			p0, p1 = p1, x[i]
		}
		w.prev2[k] = [2]float32{p0, p1}
		en /= float64(e - s)
		if w.blocks == 0 {
			w.fast[k], w.slow[k] = en, en
		}
		w.fast[k] = fbfFastAlpha*w.fast[k] + (1-fbfFastAlpha)*en
		w.slow[k] = fbfSlowAlpha*w.slow[k] + (1-fbfSlowAlpha)*en
		ratio[k] = w.fast[k] / math.Max(w.slow[k], 1e-3)
	}
	w.blocks++
	if w.blocks < fbfWarmBlocks {
		return
	}
	if w.holdLeft > 0 {
		w.holdLeft--
	}
	best := 0
	for b := 1; b < fbfBeams; b++ {
		if ratio[b] > ratio[best] {
			best = b
		}
	}
	switch {
	case w.cur == fbfNeutral:
		if ratio[best] > fbfEngageRatio && ratio[best] > fbfSwitchMargin*ratio[fbfNeutral] {
			w.pick(best)
		}
	default:
		if ratio[w.cur] < fbfReleaseRatio {
			w.lowFor++
		} else {
			w.lowFor = 0
		}
		switch {
		case w.lowFor >= fbfReleaseBlocks:
			w.pick(fbfNeutral)
		case w.holdLeft == 0 && best != w.cur && ratio[best] > fbfSwitchMargin*ratio[w.cur]:
			w.pick(best)
		}
	}
}

func (w *wakeBeam) pick(k int) {
	w.cur, w.holdLeft, w.lowFor = k, fbfHoldBlocks, 0
	w.switches++
}

// SetWakeBeam turns the filter-and-sum wake path on or off. Call from the
// goroutine that calls Process.
func (b *Beamformer) SetWakeBeam(on bool) {
	if on == (b.fbf != nil) {
		return
	}
	if on {
		b.fbf = newWakeBeam()
		log.Printf("[beam] wake beam on: %d beams, %d-tap interpolators, %d samples latency",
			fbfBeams, fbfTaps, b.fbf.off)
		return
	}
	b.fbf = nil
	log.Printf("[beam] wake beam off")
}

// WakeBeamOn reports whether the filter-and-sum wake path is enabled.
func (b *Beamformer) WakeBeamOn() bool { return b.fbf != nil }

// WakeBeam returns the filter-and-sum output for one period of the seven mic
// buffers, indexed by channel as MicChannels returns them (S16, mic gain
// applied, echo already cancelled if it is being). It must be called every
// period while the path is enabled so the filters' history stays continuous;
// use the result only while unlocked. gain is applied in the sum.
//
// Returns nil if the path is off or the buffers are not NumMics equal ones.
func (b *Beamformer) WakeBeam(mics [][]byte) []byte {
	if b.fbf == nil || len(mics) != NumMics || len(mics[0]) == 0 {
		return nil
	}
	for _, m := range mics {
		if len(m) != len(mics[0]) {
			return nil
		}
	}
	// The mics arrive with the mic gain already applied, so the sum is at unity.
	out, clipped := b.fbf.process(mics, 1, b.lockedChannel >= 0)
	b.clippedSamples += clipped
	return out
}

// WakeBeamUsable reports whether WakeBeam's output is what the pipeline should
// read now: the path is on and no turn has the mic locked.
func (b *Beamformer) WakeBeamUsable() bool { return b.fbf != nil && b.lockedChannel < 0 }

// WakeBeamIndex is the beam in use, or -1 for the centre mic. For logs and
// tests.
func (b *Beamformer) WakeBeamIndex() int {
	if b.fbf == nil || b.fbf.cur == fbfNeutral {
		return -1
	}
	return b.fbf.cur
}
