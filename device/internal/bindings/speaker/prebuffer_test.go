package speaker

import "testing"

func TestVoicePrebufferGate(t *testing.T) {
	for _, ms := range []int{128, 256, 300, 512, 1024} {
		periods := voicePrimePeriods(ms)
		if periods*2048 < ms*48 || (periods-1)*2048 >= ms*48 {
			t.Fatalf("%dms rounds to %d periods", ms, periods)
		}
		s := newAudioStream(128, make(chan struct{}))
		for i := 0; i < periods-1; i++ {
			s.ch <- make([]byte, 4096)
		}
		if s.ready(periods) {
			t.Fatalf("%dms gate opened early", ms)
		}
		s.ch <- make([]byte, 4096)
		if !s.ready(periods) {
			t.Fatalf("%dms gate did not open", ms)
		}
		s.take()
		if !s.ready(periods) {
			t.Fatal("playing stream tried to prime again")
		}
	}
	for _, ms := range []int{-1, 0, 127, 1025, 999999} {
		if voicePrimePeriods(ms) != 24 {
			t.Fatalf("unsafe setting %d did not retain legacy buffer", ms)
		}
	}
}
