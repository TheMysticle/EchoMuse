package speaker

import (
	"testing"
	"time"
)

func TestWriteMarginKeepsTheWorstOfTheMinute(t *testing.T) {
	var m writeMargin
	t0 := time.Unix(1000, 0)
	ms := time.Millisecond
	at := func(d time.Duration) time.Time { return t0.Add(d) }
	// note(written, play time, filled)
	m.note(at(2*ms), at(20*ms), at(1*ms), 256)
	m.note(at(10*ms), at(13*ms), at(5*ms), 256) // 5ms of work, 3ms to spare
	m.note(at(30*ms), at(50*ms), at(29*ms), 256)
	if m.n != 3 || m.maxWork != 5*ms || m.minMargin != 3*ms || m.tight != 1 {
		t.Fatalf("n=%d maxWork=%v minMargin=%v tight=%d", m.n, m.maxWork, m.minMargin, m.tight)
	}
	// A write after its play time is a negative margin, not a small one.
	m.note(at(40*ms), at(38*ms), at(39*ms), 256)
	if m.minMargin != -2*ms {
		t.Fatalf("minMargin=%v, want -2ms", m.minMargin)
	}
	// The minute closes and starts over.
	m.note(at(time.Minute+2*ms), at(time.Minute+20*ms), at(time.Minute), 256)
	if m.n != 0 || m.tight != 0 {
		t.Fatalf("window did not reset: n=%d tight=%d", m.n, m.tight)
	}
}
