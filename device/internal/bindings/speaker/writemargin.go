package speaker

import (
	"log"
	"time"
)

// writeMargin measures what the fill wait's reserve (sendspin fillWaitReserve,
// 8ms) is spent on: the work between a pull source's Fill returning and the
// period's write completing (output chain, volume, taps, the write itself),
// and how far ahead of its play time the period was written. A pulled period
// waits for late audio until its play time less the reserve, so the work's
// maximum is the reserve this device needs and the margin's minimum is what
// was left. Logged once a minute while pulled periods with a measured play
// time are being written; nothing otherwise.
type writeMargin struct {
	window    time.Time
	n, tight  int
	maxWork   time.Duration
	minMargin time.Duration
	frames    int
}

// writeMarginTight counts writes that finished with less than this to spare.
const writeMarginTight = 4 * time.Millisecond

func (m *writeMargin) note(now, playAt, filled time.Time, frames int) {
	if m.window.IsZero() {
		m.window = now
	}
	work, margin := now.Sub(filled), playAt.Sub(now)
	if m.n == 0 || work > m.maxWork {
		m.maxWork = work
	}
	if m.n == 0 || margin < m.minMargin {
		m.minMargin = margin
	}
	if margin < writeMarginTight {
		m.tight++
	}
	m.n++
	m.frames = frames
	if now.Sub(m.window) >= time.Minute {
		log.Printf("[speaker] write margin: periods=%d (%d frames) maxWork=%dus minMargin=%dus under%dms=%d",
			m.n, m.frames, m.maxWork.Microseconds(), m.minMargin.Microseconds(),
			writeMarginTight.Milliseconds(), m.tight)
		*m = writeMargin{window: now}
	}
}
