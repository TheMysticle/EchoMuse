package aec

import (
	"testing"
	"time"
)

// WriteFar is the speaker's echo tap: once per playback period, on the ALSA
// write loop. On the hardware reference path it has nothing to do, and it
// must not take the AEC lock to discover that.
//
// The lock is held by ProcessMicsWithRef for a WHOLE mic block — 5 frames
// across every microphone — so taking it here stalled the write loop by
// 38-53ms against a 42.7ms period budget. The DAC then ran dry:
// `mtk_pcm_I2S0dl1_pointer underflow` in dmesg followed by a PCM
// prepare/start 12-25ms later, which is the audible dropout. Measured on a
// Radar, 2026-10-11.
//
// This holds the lock directly, which a test in this package can do and the
// speaker cannot. It is the only way to assert the absence of a wait: timing
// WriteFar against a busy canceller measures the machine, not the code.
func TestWriteFarDoesNotWaitForTheLockOnTheHardwarePath(t *testing.T) {
	c := New()
	c.SetParams(true, 0, 200)
	c.SetHardwareRef(true)

	c.mu.Lock()
	done := make(chan struct{})
	go func() {
		c.WriteFar(make([]byte, 4*FrameSize))
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		c.mu.Unlock()
		t.Fatal("WriteFar waited for the AEC lock on the hardware path; " +
			"that wait is a stall of the speaker's write loop and the DAC " +
			"runs dry behind it")
	}
	c.mu.Unlock()
}

// The software tap genuinely needs the lock — it writes the reference ring —
// so the asymmetry is deliberate rather than an oversight, and this says so.
// It also proves the early return did not cost that path its work.
func TestWriteFarStillFillsTheRingOnTheSoftwarePath(t *testing.T) {
	c := New()
	c.SetParams(true, 0, 200)
	c.SetHardwareRef(false)

	c.WriteFar(make([]byte, 4*3*FrameSize)) // 3:1 decimated to FrameSize

	c.mu.Lock()
	queued := c.count
	c.mu.Unlock()
	if queued == 0 {
		t.Fatal("the software path pushed nothing: the early return is " +
			"taking a path it does not own")
	}
}

// HardwareRef is read on the hot path, so it must answer without the lock
// too — and it is the same atomic, so a stale read cannot disagree with what
// WriteFar just decided.
func TestHardwareRefIsReadableWhileTheLockIsHeld(t *testing.T) {
	c := New()
	c.SetHardwareRef(true)

	c.mu.Lock()
	defer c.mu.Unlock()
	got := make(chan bool, 1)
	go func() { got <- c.HardwareRef() }()
	select {
	case v := <-got:
		if !v {
			t.Fatal("HardwareRef reported the software path after SetHardwareRef(true)")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("HardwareRef waited for the AEC lock")
	}
}
