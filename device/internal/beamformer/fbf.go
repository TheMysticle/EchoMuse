package beamformer

// Superdirective wake beam (`wakeBeam`, off by default).
//
// The default wake path reads one microphone (see Process). With wakeBeam on,
// the unlocked path instead listens through eight fixed beams over four mics
// (fbfChannels), merged the way stock's GroupBeamMergerV2 does it: every beam
// is cancelled and given an SNR (speech-band power over a minimum-statistics
// noise floor), the eight overlapping groups of three adjacent beams are scored
// by their beams' mean SNR, the best group holds the output (1.26x margin, 96ms
// hold), and the output is the mean of its three beams plus stock's 8.3dB
// output gain. The centre mic carries the path for the first seconds, while the
// noise floors settle. Locked turns are untouched unless wakeBeamTurns says
// otherwise.
//
// The beams are superdirective (superdirective.go): plain delay-and-sum at a
// 72mm aperture gains almost nothing below 1.5kHz (tools/bf_design measures
// +0.2 to +3.1dB there), and device/CLAUDE.md records why the turn path stays
// a selector. Radar's stock beams on the same mics reach 5.6-7.5dB directivity
// there by being superdirective, which is what this follows, to within 0.15dB
// of their directivity at every frequency measured. It is an experiment to A/B
// on a device, which is why it is a setting and not a replacement.
//
// The beams are formed in the spectral domain, as stock's are: ara.go takes the
// mics' 62.5Hz-bin spectra, weights them into the eight beams, optionally
// cancels interference from each (`wakeBeamAra`), and returns the beams as
// samples, 256 samples (16ms) later. That delay is there whether or not it
// cancels, and the centre-mic fallback is delayed to match. The beams are
// scored, and the output heard, after cancelling.
//
// The delay-and-sum code below (DesignBeams) is the baseline tools/bf_design
// measures the superdirective design against; the firmware does not run it.

import (
	"fmt"
	"log"
	"math"
	"sync"
	"time"
)

const (
	// fbfBeams are spaced evenly from 0°, clockwise from 12 o'clock.
	fbfBeams = 8
	// fbfBlock is the selection granularity: 8ms, the AFE's own frame.
	fbfBlock = 128
	// fbfSettleBlocks is how long the statistics ignore the output while the
	// filters fill: 128ms against a 2ms filter, so a wide margin.
	fbfSettleBlocks = 16
	// fbfWarmBlocks is how long the path stays on the centre mic after the
	// stream starts, so the noise estimates have something to be estimates of
	// (3s).
	fbfWarmBlocks = 375
	// fbfGroupSize is how many adjacent beams the output averages: stock's
	// GroupBeamMergerV2 "3beamsGroup", eight overlapping groups [0 1 2] ..
	// [7 0 1] over beams that stock numbers round the ring.
	fbfGroupSize = 3
	// fbfHoldBlocks is the minimum time on a group before another may take over:
	// 96ms, stock's "Num Frames To Hold Beam Group".
	fbfHoldBlocks = 12
	// fbfGroupMargin is how much better another group's SNR must be than the
	// current group's before it takes over: stock's "Beam Group Margin".
	fbfGroupMargin = 1.26
	// fbfFade is the crossfade, in samples, when the output changes group.
	fbfFade = 32

	// The per-beam SNR estimate is stock's ARA_V2 one: speech-band power
	// (400-2500Hz, its "Start/Stop freq Err") smoothed fast and slow with its
	// alphas, over a noise floor that is the slow power's average for the first
	// fbfNoiseInitBlocks (625, its "Initial Noise Est Frames") and the minimum
	// over a buffer of it after that. The buffer is fbfNoiseSlots slots of
	// fbfNoiseSlotBlocks blocks, about 4s ("T frames GBM SNR switch" is 512
	// frames).
	fbfBandLoHz        = 400.0
	fbfBandHiHz        = 2500.0
	fbfPowFast         = 0.75
	fbfPowSlow         = 0.987
	fbfNoiseInitBlocks = 625
	fbfNoiseSlots      = 64
	fbfNoiseSlotBlocks = 8
	// fbfPowFloor keeps the ratio finite on digital silence (stock's "Minimum
	// ARA Power Buffer Threshold" is 1.0).
	fbfPowFloor = 1.0

	// fbfOutGainDB is stock's "ASR Output Gain": a group average gives some of
	// the talker back (the neighbours' beams see him off axis), and this puts it
	// back. Applied to every output of the wake path so the level never steps
	// when the selection moves.
	fbfOutGainDB = 8.3

	// perimeterRadiusM is the perimeter mics' distance from the centre.
	perimeterRadiusM = 0.036
	speedOfSound     = 343.0

	// fbfNeutral is the output index of the centre mic, delayed to match.
	fbfNeutral = fbfBeams

	// What the wake path is doing with a period. Listening is the normal wake
	// path; bypassed is a locked turn that does not use the beam (the
	// default), when the selection is parked and the statistics frozen; turn is
	// a locked turn that does (wakeBeamTurns), when the beam chosen at the wake
	// is held for the turn.
	modeListen = 0
	modeBypass = 1
	modeTurn   = 2
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

// Beam is one delay-and-sum beam: an FIR per microphone, summed, with the 1/M
// weight folded in. The baseline in tools/bf_design; the firmware's beams are
// SDWeights.
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

// fbfChannels are the mics the beams sum, in the order the design sees them:
// ch0, ch1, ch3 and ch4, the four Radar's AFE takes for its wake path
// (AFE.cfg Channel Map [0,1,3,4]). On this board that is a 36 x 62mm
// rectangle. The tools/bf_design sweep gave all seven mics about 1.1-1.4dB more
// wideband SNR than these four for this design (up to 8.5dB more in 1.5-4kHz
// against a side interferer) and about 1.75x the cost; the four follow the
// stock choice, so a device does the same work as the unit the design is
// measured against. Change the list and everything else follows.
var fbfChannels = []int{0, 1, 3, 4}

// wakeBeam is the wake path's state: the beam weights, the canceller, the
// centre mic's recent past for the fallback, and the selection statistics.
type wakeBeam struct {
	// sd is the beams: per-bin weights over the mics, formed in the canceller's
	// spectral domain (ara.go).
	sd *SDWeights
	// chans is every channel read: the beam mics, then the centre mic for the
	// neutral output if it is not one of them.
	chans []int
	// gain is each channel's calibration, applied as the samples are read.
	gain    [NumMics]float32
	calNote string
	check   micCheck
	// off is the delay of the neutral (centre mic) output.
	off int
	// hist holds each mic's last maxDelay samples (only the centre mic's are
	// used, for the neutral output); ext is the scratch the current period is
	// appended to.
	hist     [NumMics][]float32
	ext      [NumMics][]float32
	maxDelay int
	// out[k] is output k's signal for the period after the canceller: beams 0-7
	// and the neutral. The selection and the output both read out, so beams are
	// ranked and heard after cancelling, as in stock.
	out [fbfBeams + 1][]float32
	ara *ARA
	// araOn is the wakeBeamAra setting; the stage runs either way, so the
	// timing does not move when it changes.
	araOn bool

	// snr is each beam's speech-band power and noise floor (see beamSNR).
	snr    [fbfBeams]beamSNR
	blocks int // blocks seen, for the warm-up

	cur      int // output in use: fbfNeutral, or a group (0-7, see groupBeams)
	sel      int // the output the last emitted block used, to fade from
	holdLeft int // blocks before cur may change
	switches uint64
	// outGain is fbfOutGainDB as a factor.
	outGain float32

	// beamChans[b] is the channels beam b actually sums: those with a nonzero
	// filter. cal is whether any mic's calibration is other than unity.
	beamChans [fbfBeams][]int
	cal       bool

	// Turn mode: the beam chosen when the turn began, and where the wake came
	// from (set by Lock), so a turn with nothing engaged can start on the beam
	// nearest the mic the lock picked.
	inTurn   bool
	lockAz   float64
	lockAzOK bool
	// hp is the 80Hz high-pass stock puts on its mics before anything else
	// (AFE.cfg "HPF 80Hz @ 16K"), a 2nd-order Butterworth. It runs on the output
	// all the time so its state is continuous, and its result is used in a turn.
	hpz1, hpz2 float32

	// lastLog and suppressed rate-limit the beam-to-beam switch lines.
	lastLog    time.Time
	suppressed int
}

var (
	fbfDesignOnce  sync.Once
	fbfDesignBeams *SDWeights
)

// fbfDesign is the beam set, designed once per process: it takes long enough
// (a covariance solve per frequency per beam) that it must never run on the
// mic goroutine, and the answer never changes.
func fbfDesign() *SDWeights {
	fbfDesignOnce.Do(func() {
		start := time.Now()
		pos := make([][2]float64, len(fbfChannels))
		for i, ch := range fbfChannels {
			pos[i] = MicPosition(ch)
		}
		fbfDesignBeams = DesignSuperdirective(pos, fbfBeams, SDFloorOffsetDB)
		log.Printf("[beam] designed %d superdirective beams over mics %v in %v (diffuse field + interference ring %.1fx outside +/-%.0f deg, white-noise-gain floor %+.1fdB on stock's profile; fixed at design time, not a runtime mode)",
			fbfBeams, fbfChannels, time.Since(start).Round(time.Millisecond), SDRingWeight, SDSectorDeg, SDFloorOffsetDB)
	})
	return fbfDesignBeams
}

func newWakeBeam() *wakeBeam {
	w := &wakeBeam{sd: fbfDesign(), cur: fbfNeutral, sel: fbfNeutral,
		outGain: float32(math.Pow(10, fbfOutGainDB/20))}
	// The beams are formed in the canceller's spectral domain, so every path
	// through it has the same delay: the stage's own, which is there whether or
	// not it is cancelling. The neutral output (the centre mic) is delayed to
	// match.
	w.off = ARADelay
	w.ara = NewARA(fbfBeams)
	w.chans = append([]int(nil), fbfChannels...)
	hasCentre := false
	for _, ch := range w.chans {
		hasCentre = hasCentre || ch == centreCh
	}
	if !hasCentre {
		w.chans = append(w.chans, centreCh)
	}
	// The channels a beam actually sums: those with a nonzero weight anywhere.
	for b := 0; b < fbfBeams; b++ {
		for m, ch := range fbfChannels {
			for k := 1; k < araBins-1; k++ {
				if w.sd.W[b][m][k] != 0 {
					w.beamChans[b] = append(w.beamChans[b], ch)
					break
				}
			}
		}
	}
	w.maxDelay = w.off
	for _, ch := range w.chans {
		w.hist[ch] = make([]float32, w.maxDelay)
	}
	gains, note := loadMicCal(micCalDir)
	for ch := range gains {
		w.gain[ch] = float32(gains[ch])
		if gains[ch] != 1 {
			w.cal = true
		}
	}
	w.calNote = note
	return w
}

// fbfHP is stock's 80Hz input high-pass as direct-form biquad coefficients
// {b0, b1, b2, a1, a2}: a 2nd-order Butterworth at 16kHz, -3dB at 80Hz and
// -0.7dB at 125Hz.
var fbfHP = func() [5]float32 {
	w0 := 2 * math.Pi * 80 / sampleRate
	alpha := math.Sin(w0) / (2 * math.Sqrt2 / 2)
	c := math.Cos(w0)
	a0 := 1 + alpha
	return [5]float32{
		float32((1 + c) / 2 / a0), float32(-(1 + c) / a0), float32((1 + c) / 2 / a0),
		float32(-2 * c / a0), float32((1 - alpha) / a0),
	}
}()

// enterOrLeaveTurn handles the edges of a turn that uses the beam. Entering, a
// group already engaged by the wake is kept for the hold (it was chosen on the
// SNR of the wake itself); with none engaged the turn starts on the group
// whose middle beam is nearest the mic the lock picked. Neither edge changes
// anything else: stock's merger does not know a wake word happened.
func (w *wakeBeam) enterOrLeaveTurn(mode int) {
	switch {
	case mode == modeTurn && !w.inTurn:
		w.inTurn = true
		if w.cur == fbfNeutral && w.lockAzOK {
			best, bd := 0, 1e9
			for b, az := range w.sd.Azimuths {
				if d := math.Abs(angleDiff(az, w.lockAz)); d < bd {
					best, bd = b, d
				}
			}
			// The group whose middle beam looks that way.
			w.cur = (best + fbfBeams - 1) % fbfBeams
		}
		// The group the wake chose is kept for the hold, then the selection
		// carries on as it was.
		w.holdLeft = fbfHoldBlocks
		log.Printf("[beam] turn on %s", w.name(w.cur))
	case mode != modeTurn && w.inTurn:
		w.inTurn = false
		w.holdLeft = fbfHoldBlocks
		log.Printf("[beam] turn over, back on %s", w.name(w.cur))
	}
}

// process runs one period of NumMics S16 buffers through every beam, picks the
// output per block and returns it as S16. locked freezes the statistics and
// parks the selection on the centre mic: a turn's own voice must not move the
// baseline, and the path should start the next listen from neutral.
func (w *wakeBeam) process(mics [][]byte, gain float64, mode int) (out []byte, clipped uint64) {
	locked := mode != modeListen
	n := len(mics[0]) / 2
	for _, ch := range w.chans {
		if cap(w.ext[ch]) < w.maxDelay+n {
			w.ext[ch] = make([]float32, w.maxDelay+n)
		}
		e := w.ext[ch][:w.maxDelay+n]
		copy(e, w.hist[ch])
		g := w.gain[ch]
		checking := !w.check.done && !locked
		for i := 0; i < n; i++ {
			v := float32(int16(uint16(mics[ch][i*2]) | uint16(mics[ch][i*2+1])<<8))
			if checking {
				w.check.add(ch, v)
			}
			e[w.maxDelay+i] = v * g
		}
		copy(w.hist[ch], e[n:])
		w.ext[ch] = e
	}
	if !w.check.done && !locked {
		w.check.block(n)
		if w.check.done {
			var gains [NumMics]float64
			for ch := range gains {
				gains[ch] = float64(w.gain[ch])
			}
			w.check.report(fbfChannels, gains)
		}
	}
	for k := range w.out {
		if cap(w.out[k]) < n {
			w.out[k] = make([]float32, n)
		}
		w.out[k] = w.out[k][:n]
	}
	// The beams are formed from the mics' spectra inside the canceller. Each
	// beam mic's period of samples is the tail of its scratch.
	var micIn [NumMics][]float32
	for i, ch := range fbfChannels {
		micIn[i] = w.ext[ch][w.maxDelay:]
	}
	w.ara.SetEnabled(w.araOn)
	w.ara.ProcessMics(micIn[:len(fbfChannels)], w.sd, w.out[:fbfBeams])
	copy(w.out[fbfNeutral], w.ext[centreCh][w.maxDelay-w.off:])

	out = make([]byte, n*2)
	g := float32(gain) * w.outGain
	// Fade from the output the previous block used, which is not necessarily
	// the one in use when this call began: anything that moves cur between
	// calls (a lock, a forced beam) is faded too.
	prevSel := w.sel
	w.enterOrLeaveTurn(mode)
	for s := 0; s < n; s += fbfBlock {
		e := min(s+fbfBlock, n)
		switch mode {
		case modeBypass:
			w.cur, w.holdLeft = fbfNeutral, 0
		default:
			// Listening, and a turn heard through the beam: stock does nothing
			// different once a wake word is reported (one beamformed ASR stream
			// serves both, and the group merger keeps scoring), so the talker's
			// own voice moves the selection as it does before the wake. It cannot
			// move the noise floor, which is a minimum.
			w.step(s, e)
		}
		for i := s; i < e; i++ {
			v := w.sample(w.cur, i)
			if w.cur != prevSel && i-s < fbfFade {
				f := float32(i-s+1) / fbfFade
				v = v*f + w.sample(prevSel, i)*(1-f)
			}
			// 80Hz high-pass, always running, heard only in a turn.
			y := fbfHP[0]*v + w.hpz1
			w.hpz1 = fbfHP[1]*v - fbfHP[3]*y + w.hpz2
			w.hpz2 = fbfHP[2]*v - fbfHP[4]*y
			if mode == modeTurn {
				v = y
			}
			v *= g
			if v >= 32767 || v <= -32768 {
				clipped++
			}
			q := int32(math.Round(float64(softKnee(v))))
			out[i*2], out[i*2+1] = byte(uint16(q)), byte(uint16(q)>>8)
		}
		prevSel = w.cur
	}
	w.sel = prevSel
	return out, clipped
}

// fbfKnee is where the output starts to round off: the 8.3dB output gain puts a
// raised voice over full scale, and a hard rail there is a distortion the wake
// model never heard in training. Below the knee the path is exactly linear.
const fbfKnee = 24576

// softKnee is the identity up to fbfKnee and a tanh approach to full scale
// after it, continuous in value and slope at the knee.
func softKnee(v float32) float32 {
	a := v
	if a < 0 {
		a = -a
	}
	if a <= fbfKnee {
		return v
	}
	room := float32(32767 - fbfKnee)
	y := fbfKnee + room*float32(math.Tanh(float64((a-fbfKnee)/room)))
	if v < 0 {
		return -y
	}
	return y
}

// groupBeams are the beams group g averages: g and the next fbfGroupSize-1
// round the ring, stock's [0 1 2], [1 2 3] .. [7 0 1].
func groupBeams(g int) [fbfGroupSize]int {
	var b [fbfGroupSize]int
	for k := range b {
		b[k] = (g + k) % fbfBeams
	}
	return b
}

// sample is output k's value at i: the centre mic, or the mean of a group's
// beams (stock's group merger weights each of its beams 1/fbfGroupSize).
func (w *wakeBeam) sample(k, i int) float32 {
	if k == fbfNeutral {
		return w.out[fbfNeutral][i]
	}
	var v float32
	for _, b := range groupBeams(k) {
		v += w.out[b][i]
	}
	return v / fbfGroupSize
}

// beamSNR is one beam's SNR estimate, as stock's ARA_V2 keeps it: the power in
// the speech band smoothed fast and slow, and a noise floor taken from the slow
// one.
type beamSNR struct {
	hp, lp     biquad
	fast, slow float64
	// freeze holds the noise floor where it is: the powers are still tracked,
	// but nothing new enters the buffer the floor is the minimum of. An
	// endpointer sets it while a command is being spoken, so that a long
	// sentence cannot become the floor it is then judged against.
	freeze bool
	// ring holds the slow power sampled every fbfNoiseSlotBlocks blocks; its
	// minimum is the floor once the initial estimate period is over.
	ring  [fbfNoiseSlots]float64
	nRing int
	floor float64
}

// biquad is a direct-form-I filter, coefficients b0 b1 b2 a1 a2.
type biquad struct {
	c      [5]float64
	x1, x2 float64
	y1, y2 float64
}

func (q *biquad) run(x float64) float64 {
	y := q.c[0]*x + q.c[1]*q.x1 + q.c[2]*q.x2 - q.c[3]*q.y1 - q.c[4]*q.y2
	q.x1, q.x2, q.y2, q.y1 = x, q.x1, q.y1, y
	return y
}

// butterworth2 is a 2nd-order Butterworth, high-pass or low-pass, at hz.
func butterworth2(hz float64, high bool) biquad {
	w0 := 2 * math.Pi * hz / sampleRate
	alpha := math.Sin(w0) / math.Sqrt2
	c := math.Cos(w0)
	a0 := 1 + alpha
	var b0, b1, b2 float64
	if high {
		b0, b1, b2 = (1+c)/2, -(1 + c), (1+c)/2
	} else {
		b0, b1, b2 = (1-c)/2, 1-c, (1-c)/2
	}
	return biquad{c: [5]float64{b0 / a0, b1 / a0, b2 / a0, -2 * c / a0, (1 - alpha) / a0}}
}

func newBeamSNR() beamSNR {
	return beamSNR{hp: butterworth2(fbfBandLoHz, true), lp: butterworth2(fbfBandHiHz, false)}
}

// add folds one block of the beam's output in and returns its SNR: fast power
// over the noise floor. n is the block count since the statistics started.
func (e *beamSNR) add(x []float32, n int) float64 {
	var p float64
	for _, v := range x {
		y := e.lp.run(e.hp.run(float64(v)))
		p += y * y
	}
	return e.addPower(p/float64(len(x)), n)
}

// addPower is add for a caller that already has the block's speech-band power
// (the canceller has it from its own spectrum).
func (e *beamSNR) addPower(p float64, n int) float64 {
	if n <= 1 {
		e.fast, e.slow = p, p
	} else {
		e.fast = fbfPowFast*e.fast + (1-fbfPowFast)*p
		e.slow = fbfPowSlow*e.slow + (1-fbfPowSlow)*p
	}
	if e.freeze {
		return e.fast / math.Max(e.floor, fbfPowFloor)
	}
	if n%fbfNoiseSlotBlocks == 0 {
		e.ring[e.nRing%fbfNoiseSlots] = e.slow
		e.nRing++
	}
	switch {
	case n < fbfNoiseInitBlocks || e.nRing < fbfNoiseSlots:
		e.floor = e.slow
	default:
		e.floor = e.ring[0]
		for _, r := range e.ring[1:] {
			e.floor = math.Min(e.floor, r)
		}
	}
	return e.fast / math.Max(e.floor, fbfPowFloor)
}

// step folds block [s,e) into the statistics and moves the selection.
func (w *wakeBeam) step(s, e int) {
	var snr [fbfBeams]float64
	for b := range snr {
		if w.snr[b].lp.c[0] == 0 {
			w.snr[b] = newBeamSNR()
		}
		if w.blocks < fbfSettleBlocks {
			// The filters are still filling from silence: what comes out is their
			// step response, not the room.
			continue
		}
		snr[b] = w.snr[b].add(w.out[b][s:e], w.blocks-fbfSettleBlocks+1)
	}
	w.blocks++
	if w.blocks < fbfWarmBlocks {
		return
	}
	if w.holdLeft > 0 {
		w.holdLeft--
	}
	// A group's score is the mean SNR of its beams. That is a reading of stock's
	// config, not of its code: the formula was not found in libasp, so nothing
	// pins it and the max or the product would pass the tests as well.
	var score [fbfBeams]float64
	best := 0
	for g := range score {
		for _, b := range groupBeams(g) {
			score[g] += snr[b] / fbfGroupSize
		}
		if score[g] > score[best] {
			best = g
		}
	}
	switch {
	case w.cur == fbfNeutral:
		w.pick(best, score[best])
	case w.holdLeft == 0 && best != w.cur && score[best] > fbfGroupMargin*score[w.cur]:
		w.pick(best, score[best]/math.Max(score[w.cur], 1e-9))
	}
}

func (w *wakeBeam) pick(k int, score float64) {
	from := w.cur
	w.cur, w.holdLeft = k, fbfHoldBlocks
	w.switches++
	w.logSwitch(from, k, score)
}

func (w *wakeBeam) name(k int) string {
	if k == fbfNeutral {
		return fmt.Sprintf("centre (ch%d)", centreCh)
	}
	b := groupBeams(k)
	return fmt.Sprintf("group %d (beams %d-%d-%d, %.0f-%.0f deg)", k, b[0], b[1], b[2],
		w.sd.Azimuths[b[0]], w.sd.Azimuths[b[2]])
}

// logSwitch says what the selection did and why (score is the SNR ratio of the
// new group over the old one, or the new group's SNR when leaving the centre
// mic). Going to or from the centre mic is always logged; a switch from one
// group to another is limited to four a second, and the next line says how many
// it skipped, so a noisy room cannot flood a RAM-backed log.
func (w *wakeBeam) logSwitch(from, to int, score float64) {
	now := time.Now()
	if from != fbfNeutral && to != fbfNeutral && now.Sub(w.lastLog) < 250*time.Millisecond {
		w.suppressed++
		return
	}
	w.lastLog = now
	note := ""
	if w.suppressed > 0 {
		note = fmt.Sprintf(" (+%d group switches not logged)", w.suppressed)
		w.suppressed = 0
	}
	log.Printf("[beam] %s -> %s snr score %.2f%s", w.name(from), w.name(to), score, note)
}

// SetWakeBeam turns the superdirective wake path on or off. Call from the
// goroutine that calls Process. Turning it on starts the design in the
// background; until it is done the path passes the centre mic through, as it
// does when nothing stands out.
func (b *Beamformer) SetWakeBeam(on bool) {
	if on == b.fbfWant {
		return
	}
	b.fbfWant = on
	if !on {
		b.fbf, b.fbfPending = nil, nil
		log.Printf("[beam] wake beam off")
		return
	}
	ch := make(chan *wakeBeam, 1)
	b.fbfPending = ch
	go func() { ch <- newWakeBeam() }()
}

// SetWakeBeamAra turns the adaptive interference canceller after the beams on
// or off. It does nothing unless the wake beam itself is running, and the
// filters it has learnt are kept while it is off. Call from the goroutine that
// calls Process.
func (b *Beamformer) SetWakeBeamAra(on bool) { b.araWant = on }

// SetWakeBeamTurns makes a locked turn listen through the beam (held on the one
// chosen at the wake) instead of the single mic the lock picked. Off by
// default, and it does nothing unless the wake beam is running. Call from the
// goroutine that calls Process.
func (b *Beamformer) SetWakeBeamTurns(on bool) {
	if on != b.turnWant {
		b.turnWant = on
		log.Printf("[beam] beam for turns %s", map[bool]string{true: "on", false: "off"}[on])
	}
}

// fbfReady moves a finished design in from the background, once.
func (b *Beamformer) fbfReady() bool {
	if b.fbf != nil {
		return true
	}
	if b.fbfPending == nil {
		return false
	}
	select {
	case w := <-b.fbfPending:
		b.fbf, b.fbfPending = w, nil
		log.Printf("[beam] wake beam on: %d superdirective beams (per-bin weights over %d mics), %d samples latency, mic calibration %s",
			fbfBeams, len(fbfChannels), w.off, w.calNote)
		return true
	default:
		return false
	}
}

// WakeBeamOn reports whether the superdirective wake path is enabled. It may
// not be running yet: WakeBeam returns nil until its design is done.
func (b *Beamformer) WakeBeamOn() bool { return b.fbfWant }

// WakeBeam returns the wake path's output for one period of the seven mic
// buffers, indexed by channel as MicChannels returns them (S16, mic gain
// applied, echo already cancelled if it is being). It must be called every
// period while the path is enabled so the spectral stage's history stays
// continuous; use the result only while unlocked.
//
// Returns nil if the path is off, still being designed, or the buffers are not
// NumMics equal ones.
func (b *Beamformer) WakeBeam(mics [][]byte) []byte {
	if !b.fbfWant || !b.fbfReady() || len(mics) != NumMics || len(mics[0]) == 0 {
		return nil
	}
	for _, m := range mics {
		if len(m) != len(mics[0]) {
			return nil
		}
	}
	if b.fbf.araOn != b.araWant {
		b.fbf.araOn = b.araWant
		log.Printf("[beam] interference canceller (ARA) %s", map[bool]string{true: "on", false: "off"}[b.araWant])
	}
	// The mics arrive with the mic gain already applied, so the sum is at unity.
	b.pathFrom = b.fbf.outIndex()
	mode := modeListen
	if b.lockedChannel >= 0 {
		mode = modeBypass
		if b.turnWant {
			mode = modeTurn
		}
	}
	b.fbf.lockAz, b.fbf.lockAzOK = b.lockAz, b.lockAzOK
	out, clipped := b.fbf.process(mics, 1, mode)
	b.pathTo = b.fbf.outIndex()
	b.clippedSamples += clipped
	return out
}

// WakeBeamUsable reports whether WakeBeam's output is what the pipeline should
// read now: the path is running and either no turn has the mic locked, or one
// does and the setting for turns is on.
func (b *Beamformer) WakeBeamUsable() bool {
	return b.fbf != nil && (b.lockedChannel < 0 || b.turnWant)
}

// PinWakeGroup holds the wake path on group g (see groupBeams) until it is
// called with a negative g. It is for tools/bf_design, which has to run the
// same scene twice, with and without a talker, and subtract: the selection is
// nonlinear, so the two runs must be on the same group at every moment or the
// difference is not the talker. The firmware never calls it.
func (b *Beamformer) PinWakeGroup(g int) {
	if b.fbf == nil {
		return
	}
	if g < 0 {
		b.fbf.holdLeft = 0
		return
	}
	b.fbf.cur, b.fbf.holdLeft = g%fbfBeams, 1<<30
}

// WakeBeamIndex is the group in use (see groupBeams), or -1 for the centre mic. For logs and
// tests.
func (b *Beamformer) WakeBeamIndex() int {
	if b.fbf == nil || b.fbf.cur == fbfNeutral {
		return -1
	}
	return b.fbf.cur
}
