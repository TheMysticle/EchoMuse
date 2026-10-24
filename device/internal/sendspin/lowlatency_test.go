package sendspin

import (
	"testing"
	"time"
)

func TestArrivalLogSummary(t *testing.T) {
	var a arrivalLog
	t0 := time.Unix(1000, 0)
	for i := 0; i < 101; i++ {
		a.note(t0, int64(100+i)*1000)
	}
	if got := a.medianMs(); got != 150 {
		t.Fatalf("median = %dms, want 150", got)
	}
	if a.min != 100_000 || a.max != 200_000 {
		t.Fatalf("min=%d max=%d", a.min, a.max)
	}
	// A minute later the window is reported and starts over.
	a.note(t0.Add(time.Minute), 120_000)
	if a.n != 0 {
		t.Fatalf("window not reset: n=%d", a.n)
	}
}

// Leads outside the buckets clamp to the ends instead of indexing out of range.
func TestArrivalLogClampsExtremes(t *testing.T) {
	var a arrivalLog
	t0 := time.Unix(1000, 0)
	a.note(t0, -9_000_000)
	a.note(t0, 9_000_000)
	a.note(t0, 100_000)
	if a.n != 3 || a.min != -9_000_000 || a.max != 9_000_000 {
		t.Fatalf("n=%d min=%d max=%d", a.n, a.min, a.max)
	}
}

// The arrival log runs under the player's lock, which the speaker's pull
// needs. A minute of leads at 10ms chunks must cost microseconds, not the
// tens of milliseconds an insertion sort of them did.
func TestArrivalLogNeverStallsThePull(t *testing.T) {
	var a arrivalLog
	t0 := time.Unix(1000, 0)
	var worst time.Duration
	for i := 0; i < 6001; i++ {
		start := time.Now()
		a.note(t0.Add(time.Duration(i)*10*time.Millisecond), int64(90_000+i%40_000))
		if d := time.Since(start); d > worst {
			worst = d
		}
	}
	if worst > 5*time.Millisecond {
		t.Fatalf("one note took %v", worst)
	}
}

func TestShallowHysteresis(t *testing.T) {
	cases := []struct {
		cur  bool
		ewma int64
		want bool
	}{
		{false, 110_000, true},
		{false, 299_999, true},
		{false, 350_000, false}, // between the thresholds: unchanged
		{true, 350_000, true},
		{true, 401_000, false},
		{false, 2_000_000, false}, // Music Assistant's default lead
	}
	for _, c := range cases {
		if got := shallowNext(c.cur, c.ewma); got != c.want {
			t.Errorf("shallowNext(%v, %d) = %v, want %v", c.cur, c.ewma, got, c.want)
		}
	}
}

func TestLowLatencyFollowsLeadAndClears(t *testing.T) {
	f := &timeFilter{}
	f.init(0, 0, 0)
	p := newPlayer(f)
	// Not synchronized: a lead cannot be measured, so the ring stays deep.
	p.push(nowUs(), make([]int16, 480))
	if p.lowLatency() {
		t.Fatal("low latency reported with no clock sync")
	}
	// Forget the stream: nothing may carry over to the next one.
	p.shallow.Store(true)
	p.leadEwmaUs.Store(100_000)
	p.clear()
	if p.lowLatency() || p.leadEwmaUs.Load() != 0 {
		t.Fatal("clear left the low-latency state behind")
	}
}
