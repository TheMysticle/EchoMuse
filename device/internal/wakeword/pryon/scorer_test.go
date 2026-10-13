package pryon

import (
	"sync"
	"testing"
	"time"
)

// fake is a decoder that records what it was given.
type fake struct {
	mu      sync.Mutex
	pushes  []uint64 // timestamps
	lens    []int
	backlog int
	block   chan struct{} // when non-nil, push waits on it
	closed  int
}

func (f *fake) push(ts uint64, pcm []int16) error {
	if f.block != nil {
		<-f.block
	}
	f.mu.Lock()
	f.pushes = append(f.pushes, ts)
	f.lens = append(f.lens, len(pcm))
	f.mu.Unlock()
	return nil
}
func (f *fake) backlogMs() int { f.mu.Lock(); defer f.mu.Unlock(); return f.backlog }
func (f *fake) close()         { f.mu.Lock(); f.closed++; f.mu.Unlock() }
func (f *fake) n() int         { f.mu.Lock(); defer f.mu.Unlock(); return len(f.pushes) }

func chunk(samples int) []byte { return make([]byte, samples*2) }

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	for i := 0; i < 400; i++ {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

type cross struct {
	score, bar float32
	at         time.Time
}

func open(t *testing.T) (*Scorer, *fake, *[]cross, *sync.Mutex) {
	t.Helper()
	f := &fake{}
	var mu sync.Mutex
	var got []cross
	s := newScorer(f, func(score, bar float32, at time.Time) {
		mu.Lock()
		got = append(got, cross{score, bar, at})
		mu.Unlock()
	})
	t.Cleanup(s.Close)
	return s, f, &got, &mu
}

func TestSamplesGoToTheDecoderOnOneContinuousTimeline(t *testing.T) {
	s, f, _, _ := open(t)
	for i := 0; i < 3; i++ {
		s.PushBytesAt(chunk(1280), time.Now())
	}
	waitFor(t, "three pushes", func() bool { return f.n() == 3 })
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.pushes[0] != 0 || f.pushes[1] != 1280 || f.pushes[2] != 2560 {
		t.Errorf("timestamps %v: the decoder is given sample positions", f.pushes)
	}
}

func TestPushNeverBlocksTheMicWhenTheDecoderStalls(t *testing.T) {
	s, f, _, _ := open(t)
	f.block = make(chan struct{})
	done := make(chan struct{})
	go func() {
		for i := 0; i < queueFrames*3; i++ {
			s.PushBytesAt(chunk(1280), time.Now())
		}
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("PushBytesAt blocked on a stalled decoder: it runs on the mic loop")
	}
	if st := s.Drain(); st.Drops < uint64(queueFrames) {
		t.Errorf("drops %d, want at least %d: a frame that cannot be taken is counted", st.Drops, queueFrames)
	}
	close(f.block)
}

func TestAFullBacklogDropsInsteadOfFloodingTheDecoder(t *testing.T) {
	s, f, _, _ := open(t)
	f.backlog = maxBacklogMs + 1
	s.PushBytesAt(chunk(1280), time.Now())
	waitFor(t, "a drop", func() bool { st := s.Drain(); return st.Drops == 1 })
	if f.n() != 0 {
		t.Error("audio was pushed into an overfull decoder")
	}
	f.mu.Lock()
	f.backlog = 0
	f.mu.Unlock()
	s.PushBytesAt(chunk(1280), time.Now())
	waitFor(t, "a push", func() bool { return f.n() == 1 })
}

func TestAnAcceptCrossesAtTheCaptureTimeOfTheWordsEnd(t *testing.T) {
	s, f, got, mu := open(t)
	base := time.Unix(5000, 0)
	for i := 0; i < 4; i++ {
		s.PushBytesAt(chunk(1280), base.Add(time.Duration(i)*80*time.Millisecond))
	}
	waitFor(t, "pushes", func() bool { return f.n() == 4 })
	// the word ends in the third chunk (samples 2560..3840)
	s.onResult(TypeAccept, Keyword, 1800, 3000, 0.97)
	mu.Lock()
	defer mu.Unlock()
	if len(*got) != 1 {
		t.Fatalf("crossings %d, want 1", len(*got))
	}
	c := (*got)[0]
	if c.score != 0.97 || c.bar != AcceptBar {
		t.Errorf("score/bar %v/%v", c.score, c.bar)
	}
	if !c.at.Equal(base.Add(160 * time.Millisecond)) {
		t.Errorf("at %v: a detection is stamped with when its audio was captured", c.at.Sub(base))
	}
}

func TestOnlyAlexaWakesTheEcho(t *testing.T) {
	s, _, got, mu := open(t)
	s.onResult(TypeAccept, "STOP", 0, 100, 0.99)
	s.onResult(TypeAccept, "", 0, 100, 0.99)
	mu.Lock()
	defer mu.Unlock()
	if len(*got) != 0 {
		t.Errorf("a STOP or unnamed result woke the Echo: %v", *got)
	}
}

func TestOneUtteranceIsOneWake(t *testing.T) {
	s, _, got, mu := open(t)
	s.onResult(TypeAccept, Keyword, 0, 100, 0.95)
	s.onResult(TypeAccept, Keyword, 0, 120, 0.96)
	mu.Lock()
	n := len(*got)
	mu.Unlock()
	if n != 1 {
		t.Errorf("crossings %d, want 1: the refractory period suppresses a repeat", n)
	}
	s.mu.Lock()
	s.lastCross = time.Now().Add(-2 * DefaultRefractory)
	s.mu.Unlock()
	s.onResult(TypeAccept, Keyword, 0, 500, 0.96)
	mu.Lock()
	defer mu.Unlock()
	if len(*got) != 2 {
		t.Errorf("crossings %d, want 2 after the refractory period", len(*got))
	}
}

func TestNearMissesWakeOnlyWhileAudioPlaysAndOnlyAtThePlaybackBar(t *testing.T) {
	s, _, got, mu := open(t)
	playing := false
	s.SetBargeThreshold(0.25, func() bool { return playing })
	s.onResult(TypeNearMiss, Keyword, 0, 100, 0.89)
	mu.Lock()
	if len(*got) != 0 {
		t.Error("an idle near miss woke the Echo")
	}
	mu.Unlock()
	playing = true
	s.onResult(TypeNearMiss, Keyword, 0, 200, 0.80)
	mu.Lock()
	if len(*got) != 0 {
		t.Error("a near miss under the playback bar woke the Echo")
	}
	mu.Unlock()
	s.onResult(TypeNearMiss, Keyword, 0, 300, 0.89)
	mu.Lock()
	defer mu.Unlock()
	if len(*got) != 1 || (*got)[0].bar != PlaybackBar {
		t.Errorf("a near miss over the playback bar while playing should wake at that bar: %v", *got)
	}
}

func TestWithoutABargeThresholdThePlaybackBarIsNotUsed(t *testing.T) {
	s, _, got, mu := open(t)
	s.SetBargeThreshold(0, func() bool { return true })
	s.onResult(TypeNearMiss, Keyword, 0, 100, 0.95)
	mu.Lock()
	defer mu.Unlock()
	if len(*got) != 0 {
		t.Error("barge-in is off, so playback does not lower the bar")
	}
}

func TestBargeWindowReportsWhatWasDecidedWhilePlaying(t *testing.T) {
	s, _, _, _ := open(t)
	s.SetBargeThreshold(0.25, func() bool { return true })
	s.onResult(TypeNearMiss, Keyword, 0, 100, 0.72)
	s.onResult(TypeNearMiss, Keyword, 0, 200, 0.81)
	peak, bar, frames := s.TakeBargeWindow()
	if peak != 0.81 || bar != PlaybackBar || frames != 2 {
		t.Errorf("window %v %v %v", peak, bar, frames)
	}
	if _, _, frames := s.TakeBargeWindow(); frames != 0 {
		t.Error("the window is cleared when taken")
	}
}

func TestDrainReportsAndClearsButKeepsTheBar(t *testing.T) {
	s, f, _, _ := open(t)
	s.PushBytesAt(chunk(1280), time.Now())
	waitFor(t, "a frame", func() bool { return f.n() == 1 })
	s.onResult(TypeAccept, Keyword, 0, 100, 0.95)
	st := s.Drain()
	if st.Frames != 1 || st.Crossings != 1 || st.MaxScore != 0.95 || st.Threshold != AcceptBar {
		t.Errorf("stats %+v", st)
	}
	st = s.Drain()
	if st.Frames != 0 || st.Crossings != 0 || st.MaxScore != 0 {
		t.Errorf("not cleared: %+v", st)
	}
	if st.Threshold != AcceptBar {
		t.Errorf("the bar in force is a state, not a counter: %v", st.Threshold)
	}
}

func TestAnAcceptWhoseScoreLineWasMissedStillReportsTheBar(t *testing.T) {
	s, _, got, mu := open(t)
	s.onResult(TypeAccept, Keyword, 0, 100, 0)
	mu.Lock()
	defer mu.Unlock()
	if len(*got) != 1 || (*got)[0].score != AcceptBar {
		t.Errorf("%v", *got)
	}
}

func TestCloseReleasesTheDecoderOnceAndStaysSafeToPushTo(t *testing.T) {
	s, f, _, _ := open(t)
	s.Close()
	s.Close()
	f.mu.Lock()
	c := f.closed
	f.mu.Unlock()
	if c != 1 {
		t.Errorf("decoder closed %d times", c)
	}
	s.PushBytesAt(chunk(1280), time.Now()) // the mic goroutine still holds the scorer
	s.onResult(TypeAccept, Keyword, 0, 100, 0.99)
	if active.Load() == s {
		t.Error("a closed scorer is still the one the decoder's callbacks reach")
	}
}

func TestSettingsThatBelongToTheOpenEngineAreAcceptedAndIgnored(t *testing.T) {
	s, _, _, _ := open(t)
	s.SetThreshold(0.1)
	s.Reset()
	if !s.Ready() {
		t.Error("the decoder has no warm-up")
	}
	if s.Info() == "" {
		t.Error("Info is for the log")
	}
}
