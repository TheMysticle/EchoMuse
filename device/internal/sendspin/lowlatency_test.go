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

func TestLowLatencySurvivesAnEmptyQueueUntilStreamClear(t *testing.T) {
	p := newPlayer(syncedFilter(0))
	ts := nowUs() + 80_000
	p.push(ts, ramp(0, 512))
	if !p.Fill(make([]byte, 512*4), at(ts)) {
		t.Fatal("could not drain the queued audio")
	}
	if p.active() || !p.lowLatency() {
		t.Fatal("empty queue lost the low-latency policy")
	}
	p.clear()
	if p.lowLatency() {
		t.Fatal("ended stream retained the low-latency policy")
	}
}

// Short periods keep the drift correction at the same rate per second.
func TestShortPeriodsKeepTheDriftCorrectionRate(t *testing.T) {
	for _, divisor := range []int{2, 4} {
		frames := periodFrames / divisor
		for _, ppm := range []float64{-600, -56, 0, 56, 600} {
			p := newTestPlayer(0)
			out := make([]byte, frames*4)
			periodUs := float64(frames) * 1e6 / 48000 * (1 + ppm/1e6)
			playAt := 5_000_000.0
			var prevOut int16 = -1
			for k := 0; k < 180*divisor; k++ { // ~7.7s
				if !p.Fill(out, at(int64(playAt))) {
					t.Fatalf("ppm %v period %d: ran dry", ppm, k)
				}
				if prevOut >= 0 {
					if d := int(sample(out, 0)) - int(prevOut); d < 0 || d > 1+maxStepFrames/divisor {
						t.Fatalf("ppm %v period %d: step %d at the boundary, want at most %d",
							ppm, k, d, 1+maxStepFrames/divisor)
					}
				}
				prevOut = sample(out, frames-1)
				if k > 20*divisor {
					if e := p.stats().LastErrorUs; e > 1000 || e < -1000 {
						t.Fatalf("ppm %v period %d: error %dus outside ±1ms", ppm, k, e)
					}
				}
				playAt += periodUs
			}
			if st := p.stats(); st.Snaps != 1 {
				t.Errorf("ppm %v: %d snaps, want only the start", ppm, st.Snaps)
			}
		}
	}
}

// The output clock is built for one period length; a change of length (a
// voice turn needs whole periods) starts a new loop rather than predicting
// the next reading a whole period out when it is half of one.
func TestOutputClockFollowsThePeriodLength(t *testing.T) {
	c := &Client{player: newTestPlayer(0)}
	whole := make([]byte, periodFrames*4)
	half := make([]byte, periodFrames/2*4)
	c.Fill(whole, at(5_000_000), 0)
	if got, want := c.oc.Load().Nominal(), time.Duration(periodFrames)*time.Second/outRate; got != want {
		t.Fatalf("whole period clock nominal %v, want %v", got, want)
	}
	c.Fill(half, at(5_042_667), 0)
	if got, want := c.oc.Load().Nominal(), time.Duration(periodFrames/2)*time.Second/outRate; got != want {
		t.Fatalf("half period clock nominal %v, want %v", got, want)
	}
	first := c.oc.Load()
	c.Fill(half, at(5_064_000), 0)
	if c.oc.Load() != first {
		t.Fatal("the clock was rebuilt although the period did not change")
	}
}

// Model the streamer's 20ms submissions with up to 12ms transport lateness.
// This verifies the player budget, not the device scheduler or ALSA driver.
func TestShortPullHasArrivalJitterHeadroomAt79ms(t *testing.T) {
	for _, ringMs := range []int{48, 64} {
		p := newPlayer(syncedFilter(0))
		const leadFrames = 79 * 48
		const chunkFrames = 20 * 48
		const pullFrames = 512
		next := 0
		out := make([]byte, pullFrames*4)
		timestamp := func(frame int) int64 { return 5_000_000 + int64(frame)*1_000_000/48000 }
		for now := 0; now < 48000*5; now += pullFrames {
			for {
				// Delay every seventh batch; order stays monotonic.
				jitter := 0
				if next/chunkFrames%7 == 0 {
					jitter = 12 * 48
				}
				if next+jitter > now {
					break
				}
				p.push(timestamp(next+leadFrames), ramp(next, chunkFrames))
				next += chunkFrames
			}
			p.Fill(out, at(timestamp(now+ringMs*48)))
		}
		st := p.stats()
		if ringMs == 48 && (st.Underruns != 0 || st.Snaps != 1) {
			t.Fatalf("48ms ring: %+v; want uninterrupted playback after startup", st)
		}
		if ringMs == 64 && st.Underruns == 0 {
			t.Fatal("64ms control did not exhaust its smaller jitter margin")
		}
	}
}
