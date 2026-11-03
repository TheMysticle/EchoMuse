package server

import (
	"fmt"
	"testing"

	"github.com/wilbowes/EchoMuse/pkg/led"
)

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

func outputMuteTestServer(t *testing.T) (*Server, *recordingRing, *[]int) {
	t.Helper()
	old := statePath
	statePath = t.TempDir() + "/state.json"
	t.Cleanup(func() { statePath = old })
	ring := &recordingRing{}
	s := testServer(ring)
	s.volume.onDisplayExpire = s.paintBaseLEDs
	s.mute.repaint = s.restoreRing
	var applied []int
	s.SetVolumeApply(func(level int) { applied = append(applied, level) })
	s.SetVolume(80)
	s.volume.CancelDisplay()
	return s, ring, &applied
}

func isOutputMuteRing(l led.Led) bool {
	return l.R == outputMuteR && l.G == outputMuteG && l.B == outputMuteB
}

func TestOutputMuteSilencesKeepsTheLevelAndStaysOnTheRing(t *testing.T) {
	s, ring, applied := outputMuteTestServer(t)
	var told []string
	s.OnOutputMute(func(m bool, src string) { told = append(told, fmt.Sprint(m, " ", src)) })

	s.SetOutputMuted(true, "controller")
	if got := (*applied)[len(*applied)-1]; got != 0 {
		t.Fatalf("muted speaker plays at %d", got)
	}
	if s.VolumeLevel() != 80 {
		t.Fatalf("mute moved the volume level to %d; it must be kept for the unmute", s.VolumeLevel())
	}
	if !isOutputMuteRing(ring.last()) {
		t.Fatalf("mute ring is %+v", ring.last())
	}
	if l := ring.last(); l.R >= 180 && l.G == 0 && l.B == 0 {
		t.Fatal("output mute ring must not be the mic-mute red")
	}

	// A turn paints over it, and it comes back when the turn clears.
	listening := make([]led.Led, 12)
	for i := range listening {
		listening[i] = led.Led{ID: i, G: 180}
	}
	s.SetLEDs(listening, boolPtr(true))
	if ring.last().G != 180 {
		t.Fatal("a turn could not paint over the output-mute ring")
	}
	s.SetLEDs(blackFrame(), boolPtr(false))
	if !isOutputMuteRing(ring.last()) {
		t.Fatalf("idle ring after a turn is %+v, want the output-mute ring", ring.last())
	}

	// A volume while muted moves the level, not the sound... from a source
	// that carries mute separately (Sendspin, a group, Spotify).
	s.SetVolumeFrom(60, "sendspin", false)
	if got := (*applied)[len(*applied)-1]; got != 0 {
		t.Fatalf("a Sendspin volume unmuted the speaker: %d", got)
	}

	s.SetOutputMuted(false, "controller")
	if got := (*applied)[len(*applied)-1]; got != 60 {
		t.Fatalf("unmute plays at %d, want 60", got)
	}
	if ringDark(ring.frames[len(ring.frames)-1]) == false {
		t.Fatalf("unmuted idle ring is %+v, want dark", ring.last())
	}
	if len(told) != 2 || told[0] != "true controller" || told[1] != "false controller" {
		t.Fatalf("callback told %v", told)
	}
}

func TestOutputMuteYieldsToTheMicMuteAndComesBackAfterIt(t *testing.T) {
	s, ring, _ := outputMuteTestServer(t)
	s.SetOutputMuted(true, "controller")
	s.MuteToggle() // mic muted: red wins
	if l := ring.last(); l.R != 180 || l.B != 0 {
		t.Fatalf("mic mute over output mute shows %+v, want red", l)
	}
	s.MuteToggle()
	if !isOutputMuteRing(ring.last()) {
		t.Fatalf("after mic unmute the ring is %+v, want the output-mute ring", ring.last())
	}
}

func TestVolumeButtonsOnAMutedSpeaker(t *testing.T) {
	s, _, applied := outputMuteTestServer(t)
	s.SetOutputMuted(true, "controller")

	if s.VolumeStepDown() {
		t.Fatal("volume down on a muted speaker changed something")
	}
	if !s.OutputMuted() {
		t.Fatal("volume down unmuted")
	}
	s.VolumeStepUp()
	if s.OutputMuted() {
		t.Fatal("volume up did not unmute")
	}
	if got := (*applied)[len(*applied)-1]; got <= 80 {
		t.Fatalf("volume up unmuted at %d, want a step above 80", got)
	}
	s.volume.CancelDisplay()
}

func TestHomeAssistantVolumeUnmutesButZeroDoesNot(t *testing.T) {
	s, _, _ := outputMuteTestServer(t)
	s.SetOutputMuted(true, "controller")
	s.SetVolume(0)
	if !s.OutputMuted() {
		t.Fatal("HA volume 0 unmuted the speaker")
	}
	s.SetVolume(70)
	if s.OutputMuted() {
		t.Fatal("an HA volume did not end the mute")
	}
	s.volume.CancelDisplay()
}

func TestOutputMuteSurvivesARestart(t *testing.T) {
	s, _, _ := outputMuteTestServer(t)
	s.SetOutputMuted(true, "controller")
	st, ok := loadDeviceState(statePath)
	if !ok || !st.OutputMuted {
		t.Fatalf("output mute not persisted: %+v %v", st, ok)
	}
	s.SetOutputMuted(false, "controller")
	if st, _ := loadDeviceState(statePath); st.OutputMuted {
		t.Fatal("unmute not persisted")
	}
}
