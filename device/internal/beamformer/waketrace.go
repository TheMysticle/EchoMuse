package beamformer

// What the wake path was doing, kept per mic batch so a wake crossing can say
// what it heard through.
//
// A crossing is reported some time after the audio it fired on: the scorer
// queues up to 640ms, and the callback then runs on another goroutine. So the
// state at the moment of the log line is not the state at the crossing, and
// the question "was the beam in the path for THIS wake, and which mics fed
// it" has to be answered from the crossing's own timestamp. The mic goroutine
// notes one entry per 160ms batch; the callback looks up the batch the
// crossing frame was in.

import (
	"fmt"
	"strings"
	"sync"
	"time"
)

const (
	// pathNA marks a period the beam did not run in (off, still being
	// designed, or handed bad buffers).
	pathNA = -2
	// traceLen batches of 160ms is about 3.8s, against a scorer queue of 640ms.
	traceLen = 24
	// traceSlack lets an entry stamped a moment after the crossing frame's own
	// stamp still match it: both are taken within one pass of the mic loop.
	traceSlack = 5 * time.Millisecond
)

// WakeBeamInfo is what the wake path did with one batch of audio.
type WakeBeamInfo struct {
	// Known is false when nothing was recorded near the asked time.
	Known bool
	// Setting is wakeBeam as configured; Ready is whether its design had
	// finished; Locked is whether a turn held the mic, which bypasses the beam.
	Setting, Ready, Locked bool
	// In is the channel of the single mic the pipeline reads when the beam is
	// not in the path.
	In int
	// From and To are the output at the start and end of the batch: -1 is the
	// centre mic, 0-7 a beam, pathNA none.
	From, To int
	// Az is the engaged beam's look direction. Chans are the mics it sums.
	Az    float64
	Chans []int
	// Turn is whether this was a locked turn heard through the beam
	// (wakeBeamTurns), in which case To is the beam held for it.
	Turn bool
	// Cal is whether mic calibration other than unity was applied.
	Cal bool
	// Ara is whether the interference canceller was on; AraCut is how far it
	// had cut the engaged beam in the speech band (dB, smoothed) and
	// AraAdapting whether that beam's filters were learning.
	Ara         bool
	AraCut      float64
	AraAdapting bool
}

// String is the fragment the wake line carries.
func (i WakeBeamInfo) String() string {
	switch {
	case !i.Known:
		return "beam=? (no record of that moment)"
	case !i.Setting:
		return fmt.Sprintf("beam=off in=ch%d", i.In)
	case !i.Ready:
		return fmt.Sprintf("beam=designing in=ch%d", i.In)
	case i.Locked && !i.Turn:
		return fmt.Sprintf("beam=bypassed(turn holds the mic) in=ch%d", i.In)
	case i.To == pathNA:
		return fmt.Sprintf("beam=idle in=ch%d", i.In)
	}
	cal := "off"
	if i.Cal {
		cal = "on"
	}
	name := func(k int) string {
		if k < 0 {
			return "centre"
		}
		return fmt.Sprintf("%d", k)
	}
	state := name(i.To)
	if i.From != i.To && i.From != pathNA {
		state = name(i.From) + "->" + state
	}
	if i.Turn {
		state = "turn:" + state
	}
	ara := "off"
	if i.Ara {
		ara = "on(not in path)"
		if i.To >= 0 {
			learn := "frozen"
			if i.AraAdapting {
				learn = "learning"
			}
			ara = fmt.Sprintf("on[cut %.1fdB %s]", i.AraCut, learn)
		}
	}
	if i.To < 0 {
		// The centre mic is the whole output: the superdirective weighting is
		// not in the path at all.
		return fmt.Sprintf("beam=%s in=ch%d sd=out cal=%s ara=%s", state, centreCh, cal, ara)
	}
	chans := make([]string, len(i.Chans))
	for k, ch := range i.Chans {
		chans[k] = fmt.Sprintf("ch%d", ch)
	}
	return fmt.Sprintf("beam=%s(%.0f deg) in=%s sd=in[ring %.1fx +/-%.0f deg, floor %+.1fdB] cal=%s ara=%s",
		state, i.Az, strings.Join(chans, ","), SDRingWeight, SDSectorDeg, SDFloorOffsetDB, cal, ara)
}

type traceEntry struct {
	t    time.Time
	info WakeBeamInfo
}

type wakeTrace struct {
	mu sync.Mutex
	e  [traceLen]traceEntry
	n  int // entries ever added
}

func (t *wakeTrace) add(now time.Time, info WakeBeamInfo) {
	t.mu.Lock()
	t.e[t.n%traceLen] = traceEntry{now, info}
	t.n++
	t.mu.Unlock()
}

// at returns the newest entry stamped at or before when (plus the slack).
func (t *wakeTrace) at(when time.Time) WakeBeamInfo {
	t.mu.Lock()
	defer t.mu.Unlock()
	for k := 0; k < traceLen && k < t.n; k++ {
		e := t.e[(t.n-1-k)%traceLen]
		if !e.t.After(when.Add(traceSlack)) {
			return e.info
		}
	}
	return WakeBeamInfo{}
}

// outIndex is the output in use as WakeBeamInfo reports it.
func (w *wakeBeam) outIndex() int {
	if w.cur == fbfNeutral {
		return -1
	}
	return w.cur
}

// NoteWakePath records what the wake path did with the batch just processed.
// in is the single mic the pipeline reads when the beam is not in the path.
// Call it once per batch from the mic goroutine, after WakeBeam.
func (b *Beamformer) NoteWakePath(now time.Time, in int) {
	info := WakeBeamInfo{
		Known:   true,
		Setting: b.fbfWant,
		Ready:   b.fbf != nil,
		Locked:  b.lockedChannel >= 0,
		Turn:    b.lockedChannel >= 0 && b.turnWant,
		In:      in,
		From:    b.pathFrom,
		To:      b.pathTo,
	}
	if b.fbf != nil {
		info.Cal = b.fbf.cal
		info.Ara = b.fbf.araOn
		if info.To >= 0 {
			info.Az = b.fbf.sd.Azimuths[info.To]
			info.Chans = b.fbf.beamChans[info.To]
			info.AraCut = b.fbf.ara.CutDB(info.To)
			info.AraAdapting = b.fbf.ara.Adapting(info.To)
		}
	}
	b.pathFrom, b.pathTo = pathNA, pathNA
	b.wtrace.add(now, info)
}

// WakeBeamAt is what the wake path was doing at when: safe to call from any
// goroutine.
func (b *Beamformer) WakeBeamAt(when time.Time) WakeBeamInfo { return b.wtrace.at(when) }
