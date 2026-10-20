package sendspin

import (
	"testing"
	"time"
)

// How long audio keeps playing after the engine says it stopped, on the
// member and on the leader's own speaker. Measured from the moment the event
// is raised to the last audible sample's playback time.
func TestStopIsHeardQuicklyOnTheMemberAndOnTheLeader(t *testing.T) {
	if testing.Short() {
		t.Skip("real time")
	}
	for _, kind := range []string{"pause", "discard"} {
		c, port := startPlayer(t, "p")
		l := newTestLeader(t)
		share(t, l, c)
		l.SetMembers("G", ChannelLeft, []Member{{ID: "a", ClientID: c.ClientID(), Host: "127.0.0.1", Port: port, Channel: ChannelLeft}})
		waitFor(t, "ready", 8*time.Second, func() bool { return memberState(l, "a") == StateReady })
		src := &fakeSource{}
		l.Start(src)
		waitFor(t, "engaged", 2*time.Second, l.Engaged)
		member := startDAC(c.Fill)
		leader := startDAC(l.Fill)
		src.set(true)
		time.Sleep(3 * time.Second)

		at := time.Now()
		src.mu.Lock()
		src.playing = false
		if kind == "pause" {
			src.paused = true
		} else {
			src.disc = true
		}
		src.mu.Unlock()
		time.Sleep(1500 * time.Millisecond)
		for name, d := range map[string]*dac{"member": member, "leader": leader} {
			ts, _ := d.samples()
			t.Logf("%s: %s keeps playing %v after the stop", kind, name, ts[len(ts)-1].Sub(at).Round(time.Millisecond))
		}
		member.close()
		leader.close()
		l.Close()
	}
}

// The engine's sink flushes a few periods after a pause. They are the end of
// what was playing, not playback starting: they must not set off the replay
// of the audio the pause kept (heard as the music cutting out and then
// coming back for two seconds).
func TestAudioTrailingAPauseDoesNotReplayWhatWasKept(t *testing.T) {
	if testing.Short() {
		t.Skip("real time")
	}
	c, port := startPlayer(t, "p")
	l := newTestLeader(t)
	share(t, l, c)
	l.SetMembers("G", ChannelLeft, []Member{{ID: "a", ClientID: c.ClientID(), Host: "127.0.0.1", Port: port, Channel: ChannelLeft}})
	waitFor(t, "ready", 8*time.Second, func() bool { return memberState(l, "a") == StateReady })
	src := &fakeSource{}
	l.Start(src)
	waitFor(t, "engaged", 2*time.Second, l.Engaged)
	member, leader := startDAC(c.Fill), startDAC(l.Fill)
	defer member.close()
	defer leader.close()
	src.set(true)
	time.Sleep(3 * time.Second)

	src.mu.Lock()
	src.playing, src.paused, src.pausedState = false, true, true
	src.mu.Unlock()
	// The sink's flush arrives after the pause was reported, on the audio
	// socket, which is not ordered against the control one.
	time.Sleep(100 * time.Millisecond)
	src.mu.Lock()
	src.trail = 3
	src.mu.Unlock()
	time.Sleep(300 * time.Millisecond)
	_, m0 := member.samples()
	_, l0 := leader.samples()
	time.Sleep(2500 * time.Millisecond)
	_, m1 := member.samples()
	_, l1 := leader.samples()
	if n := len(m1) - len(m0); n != 0 {
		t.Errorf("member played %d samples after the pause was over", n)
	}
	if n := len(l1) - len(l0); n != 0 {
		t.Errorf("leader played %d samples after the pause was over", n)
	}

	// Resume still gets the kept audio, then the trailing periods, then live
	// audio, in order.
	src.mu.Lock()
	src.playing, src.pausedState = true, false
	src.mu.Unlock()
	time.Sleep(2500 * time.Millisecond)
	_, vals := member.samples()
	if n := len(vals) - len(m1); n < 24000 {
		t.Fatalf("only %d samples after resuming", n)
	}
	if n := breaks(vals[len(m1):], nextL); n > 6 {
		t.Errorf("%d discontinuities after resuming", n)
	}
}
