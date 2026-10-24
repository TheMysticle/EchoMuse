package sendspin

import (
	"math"
	"time"
)

// The next period's timestamp is fixed by already-queued ALSA audio. Missing
// network data may still arrive before that audio drains. Spend at most 24ms
// of that existing lead, leaving 24ms for DSP, the write and scheduler lateness.
// This changes when we build the period, never when we schedule it to play.
const (
	fillWaitReserve = 24 * time.Millisecond
	maxFillWait     = 24 * time.Millisecond
)

func fillWaitDeadline(now, measured time.Time, uncertain time.Duration) time.Time {
	deadline := measured.Add(-fillWaitReserve - max(uncertain, 0))
	if cap := now.Add(maxFillWait); deadline.After(cap) {
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
func (p *player) waitForPeriod(frames int, playAt, deadline time.Time) {
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
			}
			p.mu.Unlock()
			return
		}
	}
}
