package sendspin

import (
	"testing"
	"time"
)

func TestFillWaitDeadlinePreservesHardwareReserve(t *testing.T) {
	now := time.Now()
	for _, tc := range []struct{ lead, uncertainty, want time.Duration }{
		{48 * time.Millisecond, 0, 40 * time.Millisecond},
		{48 * time.Millisecond, 3 * time.Millisecond, 37 * time.Millisecond},
		{2 * time.Second, 0, 40 * time.Millisecond},
		{4 * time.Millisecond, 0, -4 * time.Millisecond},
		{48 * time.Millisecond, -time.Second, 40 * time.Millisecond},
	} {
		if got := fillWaitDeadline(now, now.Add(tc.lead), tc.uncertainty).Sub(now); got != tc.want {
			t.Fatalf("lead %v uncertainty %v: %v, want %v", tc.lead, tc.uncertainty, got, tc.want)
		}
	}
}

func waitingPlayer(t *testing.T, partial int, tight time.Time) (*player, time.Time, <-chan struct{}) {
	t.Helper()
	p := newPlayer(syncedFilter(0))
	p.push(5_000_000, ramp(0, 512+partial))
	if !p.Fill(make([]byte, 512*4), at(5_000_000)) {
		t.Fatal("startup failed")
	}
	p.shallow.Store(true)
	playAt := at(5_000_000 + 512*1_000_000/48000)
	done := make(chan struct{})
	go func() {
		p.waitForPeriod(512, playAt, time.Now().Add(time.Second), tight)
		close(done)
	}()
	limit := time.Now().Add(time.Second)
	for p.stats().Waits == 0 {
		if time.Now().After(limit) {
			t.Fatal("waiter did not start")
		}
		time.Sleep(time.Millisecond)
	}
	return p, playAt, done
}

func TestFillWaitRecoversLateDataWithoutMovingPlaybackTime(t *testing.T) {
	for _, partial := range []int{0, 128} {
		p, playAt, done := waitingPlayer(t, partial, time.Time{})
		// Simulate a packet arriving after the speaker discovered a shortage.
		// push must not be blocked by the waiter's player mutex.
		p.push(5_000_000+int64(512+partial)*1_000_000/48000, ramp(512+partial, 512-partial))
		select {
		case <-done:
		case <-time.After(time.Second):
			t.Fatal("late packet did not wake the pull")
		}
		out := make([]byte, 512*4)
		if !p.Fill(out, playAt) {
			t.Fatal("recovered period did not play")
		}
		for i := 0; i < 512; i++ {
			if got := sample(out, i); got != int16(512+i) {
				t.Fatalf("sample %d: %d, want %d", i, got, 512+i)
			}
		}
		st := p.stats()
		if st.Underruns != 0 || st.Snaps != 1 || st.Waits != 1 || st.WaitRecovered != 1 || st.WaitTimeouts != 0 {
			t.Fatalf("recovery changed the timeline or lost samples: %+v", st)
		}
	}
}

// A period that turns up after the old 16/32 budget would have given up, and
// inside the current one, is counted: it is an underrun the wider budget saved.
func TestFillWaitCountsRecoveriesPastTheTightBudget(t *testing.T) {
	for _, tc := range []struct {
		name  string
		tight time.Duration
		want  int
	}{{"inside the tight budget", time.Hour, 0}, {"past it", -time.Millisecond, 1}} {
		p, _, done := waitingPlayer(t, 0, time.Now().Add(tc.tight))
		p.push(5_000_000+512*1_000_000/48000, ramp(512, 512))
		<-done
		if st := p.stats(); st.WaitRecovered != 1 || st.WaitPastTight != tc.want {
			t.Fatalf("%s: %+v, want pastTight=%d", tc.name, st, tc.want)
		}
	}
}

func TestFillWaitClearCancelsOldStream(t *testing.T) {
	p, _, done := waitingPlayer(t, 0, time.Time{})
	p.clear()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("stream clear did not cancel the wait")
	}
	if st := p.stats(); st.WaitTimeouts != 0 || st.WaitRecovered != 0 {
		t.Fatalf("cancel counted as recovery/timeout: %+v", st)
	}
}

func TestFillWaitDeadlineDoesNotHideAnUnderrun(t *testing.T) {
	p := newTestPlayer(0)
	p.mu.Lock()
	p.q = nil
	p.playing = true
	p.shallow.Store(true)
	p.mu.Unlock()
	start := time.Now()
	p.waitForPeriod(512, at(5_000_000), start.Add(5*time.Millisecond), time.Time{})
	if elapsed := time.Since(start); elapsed > 250*time.Millisecond {
		t.Fatalf("deadline did not bound the wait: %v", elapsed)
	}
	if p.Fill(make([]byte, 512*4), at(5_000_000)) {
		t.Fatal("invented audio after timeout")
	}
	if st := p.stats(); st.WaitTimeouts != 1 || st.Underruns != 1 || st.WaitRecovered != 0 {
		t.Fatalf("timeout hid the real underrun: %+v", st)
	}
}

func TestFillAvailabilityIncludesCorrectionAndStartupSilence(t *testing.T) {
	for _, tc := range []struct {
		name    string
		queued  int
		playing bool
		errUs   int64
		missing bool
	}{
		{"exact", 512, true, 0, false},
		{"short", 511, true, 0, true},
		{"drop correction needs extra", 512, true, 200, true},
		{"drop correction covered", 514, true, 200, false},
		{"repeat correction", 510, true, -200, false},
		{"startup silence", 272, false, -5000, false},
		{"future start", 1, false, -20000, false},
		{"snap drop", 1472, true, 20000, false},
		{"snap drop short", 1471, true, 20000, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p := newPlayer(syncedFilter(0))
			p.push(5_000_000, ramp(0, tc.queued))
			p.playing = tc.playing
			if got := p.needsDataLocked(512, at(5_000_000+tc.errUs)); got != tc.missing {
				t.Fatalf("missing=%v, want %v", got, tc.missing)
			}
		})
	}
}

func TestFillWaitNormalAndNonLowLatencyPathsDoNotWait(t *testing.T) {
	p := newTestPlayer(0)
	p.shallow.Store(true)
	p.waitForPeriod(512, at(5_000_000), time.Now().Add(time.Hour), time.Time{})
	p.shallow.Store(false)
	p.q = nil
	p.playing = true
	p.waitForPeriod(512, at(5_000_000), time.Now().Add(time.Hour), time.Time{})
	if p.stats().Waits != 0 {
		t.Fatal("waited without a low-latency shortage")
	}
}
