package beamformer

// Endpointer: has the talker stopped?
//
// A wake-triggered command ends when the speech-to-text service says the
// talker has finished, and with a TV on that never comes: the TV is speech to
// it, so it waits out its own cap (15s). Measured 2026-10-12 with the TV
// running, beam and canceller on or off: 15s, 15s and 9s against ~2s in a quiet
// room. Stock's firmware does not ask a VAD to tell a talker from a TV: its
// front end cleans the stream and Amazon's cloud decides the end, so there is
// nothing on the Echo to copy. This is built for the same job from what the
// device does have.
//
// The first version judged speech by power over the MINIMUM of recent power,
// stock's floor for its canceller. The three real TV recordings showed why that
// cannot work here: a TV's dialogue swings 5-14dB above its own quiet moments
// the whole time, so it is "speech" by that measure all the time. What does
// separate a talker from the TV is level against the TV's PEAKS: in the
// recordings the command is 7-8dB above the 90th percentile of the TV's level
// around it. So the reference here is a high percentile of the speech-band power
// over the seconds BEFORE the wake word, and a block is speech when it is well
// over that. On the three recordings this ends the command at 2.2s where the
// controller took 15s, 15s and 9s, with under 1% of the TV counted as speech.
//
// The reference is fixed when the command starts, so a long sentence cannot
// raise it. It can only ever END a turn early: the controller's own ending is
// still behind it. A talker no louder than the TV's peaks is not separable.

import (
	"encoding/binary"
	"math"
	"os"
	"sort"
	"strconv"
)

// Tunables, variables so tools can sweep them and a device can be tried with
// EM_EP_* without a rebuild.
var (
	// EPQuantile is the part of the pre-wake history the reference is the
	// level of: its 97th percentile, so the TV's own loud moments sit under it.
	EPQuantile = 0.97
	// EPOn is how far over the reference a block must be to be speech, and
	// EPOff how far it must fall under to stop being: twice, and 1.4 times.
	EPOn, EPOff = 2.0, 1.4
	// EPSpeechBlocks is how much speech counts as the command having started
	// (8ms blocks): 160ms, over a click or a creak.
	EPSpeechBlocks = 20
	// EPSilenceBlocks is how long after the last speech the command is over:
	// 1.0s. Longer than a breath between words.
	EPSilenceBlocks = 125
	// EPWakeTailBlocks is how long after the wake word's own frame speech is
	// ignored, so its last syllable is not the command: 250ms.
	EPWakeTailBlocks = 31
	// EPSkipBlocks is how much of the history just before the command is left
	// out of the reference, besides the blocks since the wake word's frame: the
	// wake word itself, 2s.
	EPSkipBlocks = 250
	// EPMinHistory is how much history makes a reference: 6s. Less than that
	// and the session is left to the controller.
	EPMinHistory = 750
	// EPRefOverMedian caps the reference at this many times the history's
	// median power. A TV is loud all the time, so its 97th percentile is only
	// a few dB over its median (3.6x in the test scene, which sits at the
	// recordings' 5-14dB over the TV's quiet moments); a cough, a door or the tail of the last reply
	// is over the median by 30dB and more, and as 3% of the history it set the
	// reference. See quietRoomMoment in the test.
	EPRefOverMedian = 12.0
)

func init() {
	envF := func(name string, dst *float64) {
		if v, err := strconv.ParseFloat(os.Getenv(name), 64); err == nil && v > 0 {
			*dst = v
		}
	}
	envI := func(name string, dst *int) {
		if v, err := strconv.Atoi(os.Getenv(name)); err == nil && v > 0 {
			*dst = v
		}
	}
	envF("EM_EP_QUANTILE", &EPQuantile)
	envF("EM_EP_ON", &EPOn)
	envF("EM_EP_OFF", &EPOff)
	envI("EM_EP_SPEECH_BLOCKS", &EPSpeechBlocks)
	envI("EM_EP_SILENCE_BLOCKS", &EPSilenceBlocks)
}

// epRing is the history kept: 1500 blocks, 12s.
const epRing = 1500

type Endpointer struct {
	hp, lp biquad
	fast   float64 // speech-band power, smoothed as stock's canceller does
	n      int     // blocks seen
	acc    []float32
	hist   [epRing]float32 // the smoothed power of each of the last epRing blocks

	ref   float64 // the reference level, fixed for a session (0 = none)
	med   float64 // the median power of the history the reference came from, for the log
	speak bool
	run   int
	quiet int

	inSession bool
	ignore    int
	seen      bool
	ended     bool
	last      float64 // the last block's power over the reference, for the log
}

func NewEndpointer() *Endpointer {
	return &Endpointer{hp: butterworth2(fbfBandLoHz, true), lp: butterworth2(fbfBandHiHz, false)}
}

// Push takes S16LE mono at 16kHz, any length.
func (e *Endpointer) Push(pcm []byte) {
	for i := 0; i+1 < len(pcm); i += 2 {
		e.acc = append(e.acc, float32(int16(binary.LittleEndian.Uint16(pcm[i:]))))
		if len(e.acc) == fbfBlock {
			e.block(e.acc)
			e.acc = e.acc[:0]
		}
	}
}

func (e *Endpointer) block(x []float32) {
	var p float64
	for _, v := range x {
		y := e.lp.run(e.hp.run(float64(v)))
		p += y * y
	}
	p /= float64(len(x))
	e.n++
	if e.n <= fbfSettleBlocks { // the filters filling from nothing
		return
	}
	if e.n == fbfSettleBlocks+1 {
		e.fast = p
	}
	e.fast = fbfPowFast*e.fast + (1-fbfPowFast)*p
	e.hist[e.n%epRing] = float32(e.fast)
	if !e.inSession {
		return
	}
	e.verdict(e.fast)
}

// verdict applies one block's power to the session.
func (e *Endpointer) verdict(v float64) {
	r := v / e.ref
	e.last = r
	switch {
	case !e.speak && r >= EPOn:
		e.speak = true
	case e.speak && r < EPOff:
		e.speak = false
	}
	if e.speak {
		e.run++
		e.quiet = 0
	} else {
		e.run = 0
		e.quiet++
	}
	if e.ended {
		return
	}
	if e.ignore > 0 {
		// The wake word's own tail is not the command.
		e.ignore--
		e.run, e.quiet = 0, 0
		return
	}
	if !e.seen && e.run >= EPSpeechBlocks {
		e.seen = true
	}
	if e.seen && e.quiet >= EPSilenceBlocks {
		e.ended = true
	}
}

// StartSession is called when a command begins. sinceCrossMs is how long ago
// the wake word's last frame was: the blocks since then have already gone by,
// and the talker may have started speaking inside them, so they are looked back
// over (the first EPWakeTailBlocks of them are the wake word's own tail). The
// reference is the EPQuantile level of the history from before the wake word.
func (e *Endpointer) StartSession(sinceCrossMs int) {
	e.inSession, e.seen, e.ended, e.speak = true, false, false, false
	e.run, e.quiet, e.ref, e.med = 0, 0, 0, 0
	back := min(max(sinceCrossMs*16000/1000/fbfBlock, 0), epRing-1)
	e.ignore = max(EPWakeTailBlocks-back, 0)

	// History older than the wake word and the command's first moments.
	end := e.n - back - EPSkipBlocks // newest block index to use
	start := max(e.n-epRing+1, fbfSettleBlocks+1)
	if end-start+1 < EPMinHistory {
		return // no reference: Warm() is false, nothing is ever ended
	}
	vals := make([]float64, 0, end-start+1)
	for i := start; i <= end; i++ {
		vals = append(vals, float64(e.hist[i%epRing]))
	}
	sort.Float64s(vals)
	ref := vals[int(float64(len(vals)-1)*EPQuantile)]
	e.med = vals[len(vals)/2]
	if capRef := e.med * EPRefOverMedian; ref > capRef {
		ref = capRef
	}
	e.ref = math.Max(ref, fbfPowFloor)
	for j := EPWakeTailBlocks; j < back; j++ {
		e.verdict(float64(e.hist[(e.n-back+1+j+epRing)%epRing]))
	}
}

// EndSession returns to tracking the room.
func (e *Endpointer) EndSession() {
	e.inSession, e.seen, e.ended, e.speak = false, false, false, false
	e.ignore, e.ref, e.med = 0, 0, 0
}

// Median is the median power of the history the reference was taken from.
func (e *Endpointer) Median() float64 { return e.med }

// Warm reports that the session has a reference: there were seconds of room to
// take it from. Without one the controller's own ending is left alone.
func (e *Endpointer) Warm() bool { return e.ref > 0 }

// Ended reports that the command is over: it was heard, and then it stopped.
func (e *Endpointer) Ended() bool { return e.inSession && e.ended && e.ref > 0 }

// Heard reports that speech has been heard since the session began.
func (e *Endpointer) Heard() bool { return e.inSession && e.seen }

// State is for the log: the last block's power over the reference, the
// reference, and whether the last block was speech.
func (e *Endpointer) State() (ratio, ref float64, speech bool) {
	return e.last, e.ref, e.speak
}
