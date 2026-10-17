package server

import (
	"testing"
	"time"
)

func TestVoiceAnimationTakesPriorityOverAlertPulse(t *testing.T) {
	ring := &recordingRing{}
	s := testServer(ring)
	color := [3]uint8{0, 188, 212}
	s.SetAlertRing(&color)
	defer s.SetAlertRing(nil)
	time.Sleep(60 * time.Millisecond)
	s.SetLinkDown(true)
	s.StopAnim()
	s.StartAnim(AnimSpec{Pattern: "solid", Colors: [][3]uint8{{255, 0, 0}}})
	s.SetLEDs(frame(255, 80, 0), nil)
	got := ring.last()
	if got.R != 255 || got.G != 80 || got.B != 0 {
		t.Fatalf("voice animation did not take priority: %+v", got)
	}
	s.SetAlertRing(nil)
	s.SetLEDs(frame(255, 80, 0), nil)
	if got := ring.last(); got.R != 255 {
		t.Fatalf("alert did not release ring: %+v", got)
	}
}
