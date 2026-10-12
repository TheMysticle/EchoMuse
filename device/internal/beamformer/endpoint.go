package beamformer

// Endpointer: has the talker stopped?
//
// A wake-triggered command ends when the speech-to-text service says the
// talker has finished, and with a TV on that never comes: the TV is speech to
// it, so it waits out its own cap (15s), measured on a device 2026-10-12 with
// the TV running, whether or not the beam and canceller were in the path.
// Radar's stock firmware does not ask a VAD to tell a talker from a TV. Its
// front end gives every beam an SNR, speech-band power over a floor that is the
// MINIMUM of the last few seconds (ara.go's neighbour, fbf.go's beamSNR), and a
// steady TV, being there all along, is the floor; and what ends the utterance is
// decided in Amazon's cloud on audio that has already been through that.
//
// This is the on-device half of that, with the same estimator: a block is
// speech while its smoothed SNR is well above 1, and a command is over when
// speech was heard after the wake word and then nothing rose above the floor
// for a second. While a command is being spoken the floor is held where it was,
// so the talker cannot raise it.
//
// It is a heuristic and has the heuristic's failure: a TV whose level rises
// well above its own floor (a shout, an explosion) looks like a talker for as
// long as it lasts. It can only ever END a turn early, never hold one open: the
// controller's own ending is still there behind it.

import (
	"encoding/binary"
	"math"
	"os"
	"strconv"
)

// Tunables, variables so tools can sweep them and a device can be tried with
// EM_EP_* without a rebuild.
var (
	// EPOn and EPOff are the smoothed SNR a block must reach to become speech and
	// fall under to stop being. Stock's canceller treats an SNR under ~3.4 as
	// interference only and over ~10 as a talker; a TV that the canceller has
	// already pulled down leaves a voice well clear of 2.5.
	EPOn, EPOff = 2.5, 1.8
	// EPSpeechBlocks is how much speech counts as the command having started
	// (8ms blocks): 160ms, over a click or a creak.
	EPSpeechBlocks = 20
	// EPSilenceBlocks is how long after the last speech the command is over:
	// 1.0s. Longer than a breath between words, shorter than HA's own wait.
	EPSilenceBlocks = 125
	// EPWakeTailBlocks is how long after the wake word's own frame speech is
	// ignored, so its last syllable is not the command: 250ms.
	EPWakeTailBlocks = 31
	// epSmooth is stock's SNR smoothing (ARA_V2 keeps 0.75 old / 0.25 new).
	epSmooth = 0.75
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
	envF("EM_EP_ON", &EPOn)
	envF("EM_EP_OFF", &EPOff)
	envI("EM_EP_SPEECH_BLOCKS", &EPSpeechBlocks)
	envI("EM_EP_SILENCE_BLOCKS", &EPSilenceBlocks)
}

const epHist = 512 // blocks of speech flags kept, to look back over a session's backlog

type Endpointer struct {
	est   beamSNR
	n     int       // blocks seen
	acc   []float32 // the block being filled
	sm    float64   // smoothed SNR
	speak bool      // the last block was speech
	run   int       // consecutive speech blocks
	quiet int       // consecutive non-speech blocks
	hist  [epHist]bool

	inSession bool
	ignore    int // blocks of wake-word tail still to ignore
	seen      bool
	ended     bool
	// Last values, for the log.
	lastSNR float64
}

func NewEndpointer() *Endpointer {
	return &Endpointer{est: newBeamSNR()}
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
	e.n++
	// The first blocks are the filters filling from nothing.
	var snr float64
	if e.n > fbfSettleBlocks {
		snr = e.est.add(x, e.n-fbfSettleBlocks)
	}
	e.sm = epSmooth*e.sm + (1-epSmooth)*snr
	e.lastSNR = e.sm
	switch {
	case !e.speak && e.sm >= EPOn:
		e.speak = true
	case e.speak && e.sm < EPOff:
		e.speak = false
	}
	e.hist[e.n%epHist] = e.speak
	e.step(e.speak)
}

// step applies one block's verdict to the session.
func (e *Endpointer) step(speech bool) {
	if speech {
		e.run++
		e.quiet = 0
	} else {
		e.run = 0
		e.quiet++
	}
	if !e.inSession || e.ended {
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
// over (the first EPWakeTailBlocks of them are the wake word's own tail and are
// skipped).
func (e *Endpointer) StartSession(sinceCrossMs int) {
	e.inSession, e.seen, e.ended = true, false, false
	e.run, e.quiet = 0, 0
	e.est.freeze = true
	back := min(max(sinceCrossMs*16000/1000/fbfBlock, 0), epHist-1)
	e.ignore = max(EPWakeTailBlocks-back, 0)
	for j := EPWakeTailBlocks; j < back; j++ {
		e.step(e.hist[(e.n-back+1+j+epHist)%epHist])
	}
}

// EndSession returns to tracking the floor freely.
func (e *Endpointer) EndSession() {
	e.inSession, e.seen, e.ended = false, false, false
	e.ignore = 0
	e.est.freeze = false
}

// Warm reports that the floor means something: its initial estimate period is
// over and the buffer it is the minimum of has filled. Before that it is the
// room's level a moment ago, which a talker would already be part of, so a
// session that begins earlier is left to the controller's own ending.
func (e *Endpointer) Warm() bool {
	return e.n >= fbfNoiseInitBlocks+fbfSettleBlocks && e.est.nRing >= fbfNoiseSlots
}

// Ended reports that the command is over: it was heard, and then it stopped.
func (e *Endpointer) Ended() bool { return e.inSession && e.ended }

// Heard reports that speech has been heard since the session began.
func (e *Endpointer) Heard() bool { return e.inSession && e.seen }

// State is for the log: the smoothed SNR, the floor power, whether the last
// block was speech.
func (e *Endpointer) State() (snr, floor float64, speech bool) {
	return e.lastSNR, math.Max(e.est.floor, fbfPowFloor), e.speak
}
