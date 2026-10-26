package sendspin

import (
	"math"
	"time"
)

// The next period's timestamp is fixed by already-queued ALSA audio. Missing
// network data may still arrive before that audio drains. Spend at most 40ms
// of that existing lead, leaving 8ms for DSP, the write and scheduler lateness.
// The ring's depth adds no tolerance by itself: the deadline is measured from
// when the period plays, so a packet must arrive by then less this reserve
// whatever is queued. The reserve is the margin, and the 16ms one cost three
// underruns in 4.5 hours at 70ms lead (2026-10-25), two of them on packets
// that arrived 14 and 18ms before they were due.
// This changes when we build the period, never when we schedule it to play.
const (
	fillWaitReserve = 8 * time.Millisecond
	maxFillWait     = 40 * time.Millisecond
)

// The 16/32ms budget this replaced (268bbbc). It is computed beside the real
// one only to count the periods that arrived inside the wider budget and
// outside the old: each is an underrun the old budget would have taken, and
// the count is how the wider one earns its smaller reserve.
const (
	tightFillWaitReserve = 16 * time.Millisecond
	tightMaxFillWait     = 32 * time.Millisecond
)

func fillWaitDeadline(now, measured time.Time, uncertain time.Duration) time.Time {
	return fillWaitDeadlineWith(now, measured, uncertain, fillWaitReserve, maxFillWait)
}

func tightFillWaitDeadline(now, measured time.Time, uncertain time.Duration) time.Time {
	return fillWaitDeadlineWith(now, measured, uncertain, tightFillWaitReserve, tightMaxFillWait)
}

func fillWaitDeadlineWith(now, measured time.Time, uncertain, reserve, most time.Duration) time.Time {
	deadline := measured.Add(-reserve - max(uncertain, 0))
	if cap := now.Add(most); deadline.After(cap) {
		deadline = cap
	}
	return deadline
}

type fillPlan struct {
	errUs              int64
	drop, lead, repeat int
	snap, notDue       bool
}

// Shared by the non-mutating availability check and Fill so a correction or
// startup silence cannot make them disagree about how many samples are needed.
// Requires a nonempty queue and a synchronized filter.
func (p *player) planLocked(frames int, playAt time.Time) fillPlan {
	plan := fillPlan{errUs: toUs(playAt) - p.localTimeLocked()}
	errFrames := int(math.Round(float64(plan.errUs) * outRate / 1e6))
	maxStep := max(1, maxStepFrames*frames/stepPeriodFrames)
	switch {
	case !p.playing || plan.errUs > snapUs || plan.errUs < -snapUs:
		plan.snap = true
		if errFrames > 0 {
			plan.drop = errFrames
		} else if errFrames < 0 {
			plan.notDue = -errFrames >= frames
			plan.lead = -errFrames
		}
	case errFrames > deadbandFrames:
		plan.drop = min(errFrames, maxStep)
	case errFrames < -deadbandFrames:
		plan.repeat = min(-errFrames, maxStep)
	}
	return plan
}

func (p *player) notifyLocked() {
	select {
	case p.arrived <- struct{}{}:
	default:
	}
}

// needsDataLocked stops scanning as soon as there is enough for this pull.
// A future start or an unsynchronized clock should not block the speaker.
func (p *player) needsDataLocked(frames int, playAt time.Time) bool {
	if !p.filter.synchronized() {
		return false
	}
	if len(p.q) == 0 {
		return p.playing
	}
	plan := p.planLocked(frames, playAt)
	if plan.notDue {
		return false
	}
	needed := frames + plan.drop - plan.lead - plan.repeat + p.off
	for _, c := range p.q {
		needed -= len(c.pcm)
		if needed <= 0 {
			return false
		}
	}
	return true
}

// waitForPeriod only waits on an actual shortage, never on the normal path.
// Producers remain free to decode/push while the speaker waits. Clear/end
// wakes the waiter too; a new stream must not inherit the old wait or counters.
// A recovery after tight (zero: not counted) is also counted as WaitPastTight.
func (p *player) waitForPeriod(frames int, playAt, deadline, tight time.Time) {
	p.mu.Lock()
	if !p.shallow.Load() || !p.needsDataLocked(frames, playAt) || time.Until(deadline) <= 0 {
		p.mu.Unlock()
		return
	}
	started := time.Now()
	generation := p.generation
	p.st.Waits++
	timer := time.NewTimer(max(time.Until(deadline), 0))
	defer timer.Stop()
	for {
		p.mu.Unlock()
		select {
		case <-p.arrived:
		case <-timer.C:
		}
		p.mu.Lock()
		if p.generation != generation {
			p.mu.Unlock()
			return
		}
		missing := p.needsDataLocked(frames, playAt)
		if !missing || time.Until(deadline) <= 0 {
			p.st.WaitMaxUs = max(p.st.WaitMaxUs, time.Since(started).Microseconds())
			if missing {
				p.st.WaitTimeouts++
			} else {
				p.st.WaitRecovered++
				if !tight.IsZero() && time.Now().After(tight) {
					p.st.WaitPastTight++
				}
			}
			p.mu.Unlock()
			return
		}
	}
}
