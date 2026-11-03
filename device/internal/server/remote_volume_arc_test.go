package server

import "testing"

func ringFrameCount(r *recordingRing) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.frames)
}

func TestRemoteVolumeArcIsOptInAndOnlyShowsForAChange(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)

	s.SetVolume(80)
	if got := ringFrameCount(ring); got != 0 {
		t.Fatalf("default-off remote set painted %d frames", got)
	}

	s.SetRemoteVolumeArc(true)
	s.SetVolume(81)
	if got := ringFrameCount(ring); got != 1 {
		t.Fatalf("enabled remote change painted %d frames, want 1", got)
	}
	if !s.volume.DisplayActive() {
		t.Fatal("enabled remote change did not hold the volume display")
	}

	s.SetVolume(81)
	if got := ringFrameCount(ring); got != 1 {
		t.Fatalf("duplicate remote level repainted the ring: %d frames", got)
	}
	s.volume.CancelDisplay()
}

func TestRemoteVolumeArcDoesNotAffectSeedOrPhysicalButtons(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)
	s.SetRemoteVolumeArc(true)

	s.SeedVolume(80)
	if got := ringFrameCount(ring); got != 0 {
		t.Fatalf("boot-time seed painted %d frames", got)
	}

	s.VolumeStepUp()
	if got := ringFrameCount(ring); got != 1 {
		t.Fatalf("physical button painted %d frames, want 1", got)
	}
	s.volume.CancelDisplay()
}

func TestRemoteVolumeArcSkipsMutedLevel(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)
	s.SetRemoteVolumeArc(true)

	s.SetVolume(80)
	if got := ringFrameCount(ring); got != 1 {
		t.Fatalf("enabled non-zero remote change painted %d frames, want 1", got)
	}
	s.volume.CancelDisplay()

	s.SetVolume(0)
	if got := s.VolumeLevel(); got != 0 {
		t.Fatalf("muted remote level was not applied: got %d", got)
	}
	if got := ringFrameCount(ring); got != 1 {
		t.Fatalf("muted remote level repainted the ring: %d frames", got)
	}
	if s.volume.DisplayActive() {
		t.Fatal("muted remote level held the volume display")
	}
}

func TestOutputMuteShowsItsOwnRingNotRedAndSilences(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)
	s.SetRemoteVolumeArc(true)
	s.SetVolume(80)
	s.volume.CancelDisplay()
	before := ringFrameCount(ring)

	s.MuteOutput(0)
	if got := s.VolumeLevel(); got != 0 {
		t.Fatalf("output mute did not silence: level %d", got)
	}
	if got := ringFrameCount(ring); got != before+1 {
		t.Fatalf("output mute painted %d frames, want 1", got-before)
	}
	l := ring.last()
	if l.R != outputMuteR || l.G != outputMuteG || l.B != outputMuteB {
		t.Fatalf("output mute ring is %d,%d,%d", l.R, l.G, l.B)
	}
	if l.R >= 180 && l.G == 0 && l.B == 0 {
		t.Fatal("output mute ring must not be the mic-mute red")
	}
	if !s.volume.DisplayActive() {
		t.Fatal("output mute ring did not hold the display")
	}
	s.volume.CancelDisplay()

	// Unmute is the restored level: the ordinary cyan arc.
	s.SetVolume(80)
	if l := ring.last(); l.R != 0 || l.G == 0 || l.G != l.B {
		t.Fatalf("unmute did not draw the cyan arc: %d,%d,%d", l.R, l.G, l.B)
	}
	s.volume.CancelDisplay()
}

func TestOutputMuteRingFollowsRemoteVolumeArcButStillSilences(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)
	s.SetVolume(80)

	s.MuteOutput(0)
	if got := s.VolumeLevel(); got != 0 {
		t.Fatalf("output mute did not silence: level %d", got)
	}
	if got := ringFrameCount(ring); got != 0 {
		t.Fatalf("output mute painted %d frames with remoteVolumeArc off", got)
	}
}
