package client

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func withCapturePaths(t *testing.T) (req, out string) {
	t.Helper()
	dir := t.TempDir()
	req, out = filepath.Join(dir, "capture.req"), filepath.Join(dir, "capture.raw")
	oldReq, oldOut := captureReqPath, captureOutPath
	captureReqPath, captureOutPath = req, out
	t.Cleanup(func() { captureReqPath, captureOutPath = oldReq, oldOut })
	return
}

// No request: nothing is written and nothing is opened.
func TestCaptureDoesNothingWithoutARequest(t *testing.T) {
	_, out := withCapturePaths(t)
	var c rawCapture
	now := time.Now()
	for i := 0; i < 10; i++ {
		c.feed(make([]byte, 69120), now.Add(time.Duration(i)*time.Second))
	}
	if _, err := os.Stat(out); err == nil {
		t.Error("an output file exists with no request")
	}
}

// A request for N seconds writes exactly N seconds of the buffers it is given
// and removes the request, and a bad number falls back to the default.
func TestCaptureWritesTheRequestedSecondsThenRemovesTheRequest(t *testing.T) {
	req, out := withCapturePaths(t)
	if err := os.WriteFile(req, []byte("2\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	var c rawCapture
	buf := make([]byte, 69120) // 160ms
	for i := range buf {
		buf[i] = byte(i)
	}
	now := time.Now()
	for i := 0; i < 40; i++ { // 6.4s offered, 2s asked for
		c.feed(buf, now.Add(time.Duration(i)*160*time.Millisecond))
	}
	st, err := os.Stat(out)
	if err != nil {
		t.Fatal(err)
	}
	if want := int64(2 * captureBytesPerSecond); st.Size() != want {
		t.Errorf("wrote %d bytes, want exactly %d", st.Size(), want)
	}
	if _, err := os.Stat(req); err == nil {
		t.Error("the request was not removed")
	}
}

func TestCaptureClampsAndDefaults(t *testing.T) {
	req, _ := withCapturePaths(t)
	for in, want := range map[string]int{"": captureDefaultSeconds, "x": captureDefaultSeconds, "0": captureDefaultSeconds, "500": captureMaxSeconds, "7": 7} {
		os.WriteFile(req, []byte(in), 0o644)
		var c rawCapture
		if !c.begin() {
			t.Fatalf("%q: no capture begun", in)
		}
		if c.left != want*captureBytesPerSecond {
			t.Errorf("%q: %d seconds, want %d", in, c.left/captureBytesPerSecond, want)
		}
		c.finish(false)
	}
}
