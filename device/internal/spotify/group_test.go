package spotify

import (
	"testing"
	"time"
)

func TestRingReadWaitTakesAWholePeriodOrNothing(t *testing.T) {
	g := newRing(48000)
	out := make([]byte, groupChunk)
	if g.readWait(out, 0) {
		t.Fatal("read from an empty ring")
	}
	g.write(make([]byte, groupChunk-4))
	if g.readWait(out, 20*time.Millisecond) {
		t.Fatal("took a partial period: that splices silence into the waveform")
	}
	if g.len() != groupChunk-4 {
		t.Fatalf("a refused read consumed %d bytes", groupChunk-4-g.len())
	}
	g.write(make([]byte, 4))
	if !g.readWait(out, 0) {
		t.Fatal("a whole period was there")
	}
}

func TestRingReadWaitWakesWhenAudioArrives(t *testing.T) {
	g := newRing(48000)
	out := make([]byte, groupChunk)
	go func() {
		time.Sleep(30 * time.Millisecond)
		g.write(make([]byte, groupChunk))
	}()
	start := time.Now()
	if !g.readWait(out, 2*time.Second) {
		t.Fatal("never woke")
	}
	if d := time.Since(start); d > time.Second {
		t.Errorf("woke after %v: it polled instead of being signalled", d)
	}
}

func TestRingWatchCountReturnsToZero(t *testing.T) {
	g := newRing(48000)
	g.readWait(make([]byte, groupChunk), 5*time.Millisecond)
	if g.watch.Load() != 0 {
		t.Error("a finished reader left the ring signalling")
	}
}

func TestRingWriteWithoutAWatcherDoesNotSignal(t *testing.T) {
	g := newRing(48000)
	g.write(make([]byte, 1000))
	select {
	case <-g.notify:
		t.Error("write signalled with nobody listening")
	default:
	}
}

func TestEventsReportAFlushOnceAndPauseOnce(t *testing.T) {
	p := New(Config{})
	if d, pa := p.Events(); d || pa {
		t.Fatal("events before anything happened")
	}
	p.ring.flush()
	if d, _ := p.Events(); !d {
		t.Fatal("a flush was not reported")
	}
	if d, _ := p.Events(); d {
		t.Fatal("a flush was reported twice")
	}

	p.apply(&event{Event: "state", State: "playing"})
	p.apply(&event{Event: "state", State: "paused"})
	if _, pa := p.Events(); !pa {
		t.Fatal("a pause was not reported")
	}
	if _, pa := p.Events(); pa {
		t.Fatal("a pause was reported twice")
	}
}

func TestSendThatFlushesIsADiscard(t *testing.T) {
	p := New(Config{})
	p.Send("next", nil) // no engine connected: it still discards what is queued
	if d, _ := p.Events(); !d {
		t.Error("a skip did not count as audio thrown away")
	}
}

// A pause alone must not discard: the leader keeps what it had sent ahead.
func TestPauseIsNotADiscard(t *testing.T) {
	p := New(Config{})
	p.apply(&event{Event: "state", State: "playing"})
	p.apply(&event{Event: "state", State: "paused"})
	if d, _ := p.Events(); d {
		t.Error("a pause discarded the audio that is to be kept")
	}
}

// Paused in one track, resumed in another: the kept audio is the old track's.
func TestResumingIntoADifferentTrackDiscards(t *testing.T) {
	p := New(Config{})
	p.apply(&event{Event: "track", URI: "spotify:track:a", Name: "A"})
	p.apply(&event{Event: "state", State: "playing"})
	p.apply(&event{Event: "state", State: "paused"})
	p.Events()

	p.apply(&event{Event: "track", URI: "spotify:track:b", Name: "B"})
	if d, _ := p.Events(); !d {
		t.Error("a different track after a pause kept the old track's tail")
	}
}

func TestResumingTheSameTrackKeeps(t *testing.T) {
	p := New(Config{})
	p.apply(&event{Event: "track", URI: "spotify:track:a", Name: "A"})
	p.apply(&event{Event: "state", State: "playing"})
	p.apply(&event{Event: "state", State: "paused"})
	p.Events()
	p.apply(&event{Event: "state", State: "playing"})
	if d, _ := p.Events(); d {
		t.Error("resuming the same track discarded the kept audio")
	}
	// ...and a later natural change of track is not mistaken for a skip.
	p.apply(&event{Event: "track", URI: "spotify:track:b", Name: "B"})
	if d, _ := p.Events(); d {
		t.Error("a natural track change cut the end of the previous track")
	}
}

func TestNaturalTrackChangeWhilePlayingIsNotADiscard(t *testing.T) {
	p := New(Config{})
	p.apply(&event{Event: "track", URI: "spotify:track:a"})
	p.apply(&event{Event: "state", State: "playing"})
	p.apply(&event{Event: "track", URI: "spotify:track:b"})
	p.apply(&event{Event: "state", State: "stopped"})
	if d, pa := p.Events(); d || pa {
		t.Errorf("the end of a track was taken for a skip or a pause (discard=%v pause=%v)", d, pa)
	}
}

func TestIdleIgnoresATailShorterThanAPeriod(t *testing.T) {
	p := New(Config{})
	if !p.Idle() {
		t.Error("a fresh player is not idle")
	}
	p.ring.write(make([]byte, groupChunk-1))
	if !p.Idle() {
		t.Error("the end of a track left the engine looking busy for ever")
	}
	p.ring.write(make([]byte, 1))
	if p.Idle() {
		t.Error("a whole period queued reads as idle")
	}
}
