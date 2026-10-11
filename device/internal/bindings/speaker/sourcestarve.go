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

// tally is a counter with a rate-limited "say something now" decision.
// Shared by the two things worth counting per period, so the limiting rule
// exists once.
type tally struct {
	count   atomic.Uint64
	lastLog atomic.Int64 // unix nanos of the last log line, 0 for never
}

func (t *tally) add(now time.Time) (uint64, bool) {
	total := t.count.Add(1)
	last := t.lastLog.Load()
	if last != 0 && now.Sub(time.Unix(0, last)) < StarveLogEvery {
		return total, false
	}
	// Compare-and-swap so two write loops cannot both decide to log. There
	// is only one today, and a counter that quietly depends on that is how a
	// second one arrives and doubles the log.
	if !t.lastLog.CompareAndSwap(last, now.UnixNano()) {
		return total, false
	}
	return total, true
}

func (t *tally) total() uint64 { return t.count.Load() }

// SourceStarve counts periods the pull source had audio for and could not
// fill.
type SourceStarve struct {
	tally
}

// AlsaXrun counts periods on which the substream was in XRUN — the DAC had
// run dry before we got there.
//
// This is the click, not a symptom of it, and it was invisible: pcmDelay
// refuses any state but RUNNING, so pullSource gave up before asking the
// source for audio and the starve counter never saw it. Unlike a starve
// there is no innocent case to exclude — an XRUN is always a fault.
type AlsaXrun struct {
	tally
}

// Note records an XRUN. Returns the running total and whether to log.
func (x *AlsaXrun) Note(now time.Time) (uint64, bool) { return x.add(now) }

// Total is every XRUN counted since boot.
func (x *AlsaXrun) Total() uint64 { return x.total() }

// Note records a period the source declined.
//
// playedNs is when it last filled one, 0 for never. Returns the running
// total and whether this is the moment to log. A decline that is not a
// starve returns (total so far, false) and counts nothing.
func (s *SourceStarve) Note(now time.Time, playedNs int64) (uint64, bool) {
	if playedNs == 0 || now.Sub(time.Unix(0, playedNs)) > StarveWindow {
		return s.total(), false
	}
	return s.add(now)
}

// Total is every starve counted since boot.
func (s *SourceStarve) Total() uint64 { return s.total() }

// AlsaMargin is how close the DAC came to running dry, which is the question
// an XRUN check cannot answer.
//
// tinyalsa's pcm_write handles -EPIPE itself: it bumps a private counter,
// calls pcm_prepare and retries, returning success. The substream is back to
// RUNNING microseconds later, on the same goroutine that would read the
// status file — so `state: XRUN` is essentially never observable from procfs
// and a detector for it reports nothing while clicks are audible (measured
// on a Radar 2026-10-11, exactly that).
//
// The margin is observable on every ordinary period. pullSource already
// reads the queued frames to place audio in time, so this costs a compare
// and is the same reasoning behind min_depth on the channel-fed planes:
// an underrun is binary and rare, the margin says how close every period
// came and degrades visibly before anything is audible.
type AlsaMargin struct {
	tally
	primed atomic.Bool
	lowest atomic.Int64
}

// MarginFloorFrames is the depth below which a period is worth reporting:
// one 2048-frame period at 48kHz, 42.7ms. Under that, a single late write
// is silence rather than a near miss.
const MarginFloorFrames = 2048

// Note records the frames queued ahead of the DAC for this period. Returns
// the lowest depth seen and whether to log now.
//
// A stream has to be PRIMED first — the buffer is legitimately shallow while
// it fills, and counting that would report a fault at the start of every
// track, the same innocent case SourceStarve excludes.
func (m *AlsaMargin) Note(now time.Time, frames int) (int, bool) {
	if frames >= MarginFloorFrames {
		m.primed.Store(true)
		return int(m.lowest.Load()), false
	}
	if !m.primed.Load() {
		return int(m.lowest.Load()), false
	}
	for {
		old := m.lowest.Load()
		if old != 0 && old <= int64(frames) {
			break
		}
		if m.lowest.CompareAndSwap(old, int64(frames)) {
			break
		}
	}
	total, shout := m.add(now)
	_ = total
	return int(m.lowest.Load()), shout
}

// Lowest is the shallowest depth seen since boot, 0 if never below the floor.
func (m *AlsaMargin) Lowest() int { return int(m.lowest.Load()) }

// Thin is how many periods have been below the floor since boot.
func (m *AlsaMargin) Thin() uint64 { return m.total() }
