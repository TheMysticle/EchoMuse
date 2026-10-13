//go:build pryonlive

package pryon

import (
	"os"
	"sync"
	"testing"
	"time"
)

// TestLiveDecoder drives the real library. It exists for the PC harness in
// tools/pryon_probe (a test binary built for android/arm and run under
// qemu-user against a copy of a device's /system) and for running on an Echo;
// it cannot run anywhere else. EM_PRYON_TEST_RAW is a 16kHz mono int16 file and
// EM_PRYON_TEST_WANT how many wake words it contains.
func TestLiveDecoder(t *testing.T) {
	path := os.Getenv("EM_PRYON_TEST_RAW")
	if path == "" {
		t.Skip("EM_PRYON_TEST_RAW not set")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	var scores []float32
	s, err := Open(func(score, bar float32, at time.Time) {
		mu.Lock()
		scores = append(scores, score)
		mu.Unlock()
	})
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Log(s.Info())
	const chunk = 1280 * 2
	for i := 0; i+chunk <= len(raw); i += chunk {
		s.PushBytesAt(raw[i:i+chunk], time.Now())
		time.Sleep(80 * time.Millisecond) // microphone speed
	}
	time.Sleep(3 * time.Second)
	st := s.Drain()
	s.Close()
	mu.Lock()
	defer mu.Unlock()
	t.Logf("crossings %d scores %v frames %d drops %d errors %d (%s)", len(scores), scores, st.Frames, st.Drops, st.Errors, st.LastErr)
	want := 4
	if v := os.Getenv("EM_PRYON_TEST_WANT"); v != "" {
		want = int(v[0] - '0')
	}
	if len(scores) != want {
		t.Errorf("%d wake words, want %d", len(scores), want)
	}
	if st.Drops != 0 {
		t.Errorf("%d frames dropped at microphone speed", st.Drops)
	}
}
