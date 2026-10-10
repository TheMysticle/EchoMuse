package speaker

import (
	"sync/atomic"
	"time"
)

// Starve accounting for the music PULL source (Sendspin, Spotify).
//
// The channel-fed planes have had this since v7: an underrun is counted in
// audioStream and logged as "[speaker] UNDERRUN: … injecting silence", and
// every stream reports periods/underruns/minDepth when it ends. The pull
// source had NONE of it — `pullSource` returns nil when Fill declines and
// nothing counts, logs or reports it. So a brief dropout on Spotify or
// Sendspin audio was structurally invisible: no counter, no log line,
// nothing in a support bundle. A click nobody can see is a click nobody can
// fix, and "is it the source, the link, or the scheduler" cannot even be
// asked without this.
//
// Cost is the class the project already accepts for per-period work: one
// atomic add on the path that just declined to produce audio, and a log line
// at most every StarveLogEvery. The rate limit is not politeness — the
// device log is MediaTek's fixed-size ram_console, the only crash channel
// this kernel has, so a starving source logging per period would evict the
// evidence of whatever caused it (the same cost that took the ALS poll from
// 1Hz to 5s).

// StarveWindow is how recently the source must have filled a period for a
// decline to count as a starve.
//
// It is what separates "it had audio and missed" from the two innocent
// cases, and both are ordinary. A source that has never filled anything is
// starting up — Sendspin places audio in time and legitimately declines
// every period until its scheduled start — and counting those would report
// a starve at the top of every track. A source that filled a period a long
// time ago has effectively stopped, with `Active` merely lingering.
const StarveWindow = time.Second

// StarveLogEvery bounds the log line. The COUNT is the diagnosis, not each
// event, so one line carrying a running total answers the question at a
// fraction of the log budget.
const StarveLogEvery = 10 * time.Second

// SourceStarve counts periods the pull source had audio for and could not
// fill.
type SourceStarve struct {
	count   atomic.Uint64
	lastLog atomic.Int64 // unix nanos of the last log line, 0 for never
}

// Note records a period the source declined.
//
// playedNs is when it last filled one, 0 for never. Returns the running
// total and whether this is the moment to log. A decline that is not a
// starve returns (total so far, false) and counts nothing.
func (s *SourceStarve) Note(now time.Time, playedNs int64) (uint64, bool) {
	if playedNs == 0 || now.Sub(time.Unix(0, playedNs)) > StarveWindow {
		return s.count.Load(), false
	}
	total := s.count.Add(1)
	last := s.lastLog.Load()
	if last != 0 && now.Sub(time.Unix(0, last)) < StarveLogEvery {
		return total, false
	}
	// Compare-and-swap so two write loops cannot both decide to log. There
	// is only one today, and a counter that quietly depends on that is how a
	// second one arrives and doubles the log.
	if !s.lastLog.CompareAndSwap(last, now.UnixNano()) {
		return total, false
	}
	return total, true
}

// Total is every starve counted since boot.
func (s *SourceStarve) Total() uint64 { return s.count.Load() }
