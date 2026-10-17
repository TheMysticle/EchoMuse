package cpuprofile

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func withPaths(t *testing.T) (req, out string) {
	t.Helper()
	dir := t.TempDir()
	req, out = filepath.Join(dir, "cpuprofile.req"), filepath.Join(dir, "cpu.pprof")
	oldReq, oldOut := reqPath, outPath
	reqPath, outPath = req, out
	t.Cleanup(func() { reqPath, outPath = oldReq, oldOut })
	return
}

func TestNoRequestWritesNothing(t *testing.T) {
	_, out := withPaths(t)
	if _, ok := request(); ok {
		t.Fatal("request reported with no file")
	}
	if _, err := os.Stat(out); err == nil {
		t.Error("an output file exists with no request")
	}
}

func TestRequestSecondsAreClampedAndTheRequestIsConsumed(t *testing.T) {
	req, _ := withPaths(t)
	for in, want := range map[string]int{"5\n": 5, "": defaultSeconds, "junk": defaultSeconds, "0": defaultSeconds, "-3": defaultSeconds, "9999": maxSeconds} {
		if err := os.WriteFile(req, []byte(in), 0o644); err != nil {
			t.Fatal(err)
		}
		got, ok := request()
		if !ok || got != want {
			t.Errorf("%q: got %d ok=%v, want %d", in, got, ok, want)
		}
		if _, err := os.Stat(req); err == nil {
			t.Errorf("%q: request not removed", in)
		}
	}
}

func TestWatchWritesAProfileAndRemovesTheRequest(t *testing.T) {
	req, out := withPaths(t)
	if err := os.WriteFile(req, []byte("1"), 0o644); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { Watch(ctx); close(done) }()
	deadline := time.Now().Add(6 * time.Second)
	for time.Now().Before(deadline) {
		if fi, err := os.Stat(out); err == nil && fi.Size() > 0 {
			if _, err := os.Stat(req); err == nil {
				t.Fatal("request still present")
			}
			cancel()
			<-done
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	cancel()
	t.Fatal("no profile written")
}
