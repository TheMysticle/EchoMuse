// Package pryon runs the Echo's own wake word decoder (Radar's libpryon.so and
// the ALEXA model package from the device's /system) as an on-device engine
// beside the open-source one in package shadow.
//
// It is an OPT-IN for one platform, and it is never linked: the library is
// dlopen'd at run time from the device's own firmware and the model is read in
// place, so a board without them just reports the engine unavailable and the
// firmware carries on with the open model. Nothing of Amazon's is in this
// repository. See tools/pryon_probe for the calling convention, which was read
// off the way Amazon's libWakeWordManager calls the same functions, and for the
// PC harness it was measured with.
//
// This file is the part that needs no library: deciding what a detection means.
package pryon

import (
	"math"
	"strconv"
	"strings"
	"sync"
	"time"
)

// ModelName is the owwModel value that selects this engine. The open models
// are named after their files; this one is not a file, so it cannot collide.
const ModelName = "pryon_alexa"

// IsModel reports whether an owwModel value selects this engine.
func IsModel(owwModel string) bool { return strings.TrimSpace(owwModel) == ModelName }

// The decoder's own bars, from the model package's kw.cfg.json: the ALEXA
// spotter accepts at 0.923, at 0.864 while audio is playing, and reports a
// near miss from 0.69. They are the model's, not ours to tune: it was trained
// and calibrated against them, which is why the owwThreshold setting does not
// apply to this engine.
const (
	AcceptBar   float32 = 0.923
	PlaybackBar float32 = 0.864
	NotifyBar   float32 = 0.69
)

// Result types as the decoder reports them (word 9 of its result struct).
const (
	TypeNearMiss uint32 = 0
	TypeAccept   uint32 = 2
)

// Keyword is the word this engine acts on. The package also spots STOP, which
// is for stopping playback and not for waking the Echo.
const Keyword = "ALEXA"

// ParseScore reads classifier_score out of the decoder's log line. The result
// struct carries the keyword, its position and its type but not the score; the
// decoder logs the score on the same thread just before it calls back, and the
// text is stable ("classifier_score=0.998347,active_threshold=0.923,...").
func ParseScore(msg string) (float32, bool) {
	const key = "classifier_score="
	i := strings.Index(msg, key)
	if i < 0 {
		return 0, false
	}
	rest := msg[i+len(key):]
	if j := strings.IndexAny(rest, ", \t"); j >= 0 {
		rest = rest[:j]
	}
	v, err := strconv.ParseFloat(rest, 32)
	if err != nil || math.IsNaN(v) || math.IsInf(v, 0) {
		return 0, false
	}
	return float32(v), true
}

// Judge decides whether a decoder result is a wake, and the bar it was held to.
//
// An Accept is the decoder's own decision at its own bar. While audio plays,
// stock lowers that bar to PlaybackBar through a client property; this engine
// does not set the property, so it does the same on the near misses instead: a
// near miss at or over the playback bar counts. A near miss otherwise, and a
// word other than ALEXA, never does.
func Judge(typ uint32, score float32, playing bool) (crossed bool, bar float32) {
	bar = AcceptBar
	if playing {
		bar = PlaybackBar
	}
	switch typ {
	case TypeAccept:
		return true, bar
	case TypeNearMiss:
		return playing && score >= PlaybackBar, bar
	}
	return false, bar
}

// timeRing maps a sample position back to when that audio was captured, so a
// detection (which names samples) can be stamped with the capture time the
// arbitration and the private-listening session start are measured from.
type timeRing struct {
	mu   sync.Mutex
	end  []uint64
	at   []time.Time
	head int
	n    int
}

const timeRingLen = 512 // 80ms chunks: 40s

func newTimeRing() *timeRing {
	return &timeRing{end: make([]uint64, timeRingLen), at: make([]time.Time, timeRingLen)}
}

// add records that the audio ending at sample `end` was captured at `at`.
func (r *timeRing) add(end uint64, at time.Time) {
	r.mu.Lock()
	r.end[r.head], r.at[r.head] = end, at
	r.head = (r.head + 1) % timeRingLen
	if r.n < timeRingLen {
		r.n++
	}
	r.mu.Unlock()
}

// when is the capture time of the chunk containing sample s; fallback is
// returned when the sample is older than the ring or not yet pushed.
func (r *timeRing) when(s uint64, fallback time.Time) time.Time {
	r.mu.Lock()
	defer r.mu.Unlock()
	for k := 0; k < r.n; k++ {
		i := (r.head - r.n + k + timeRingLen) % timeRingLen
		if r.end[i] >= s {
			return r.at[i]
		}
	}
	return fallback
}
