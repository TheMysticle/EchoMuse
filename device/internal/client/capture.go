package client

// On-demand raw capture of the microphone array.
//
// Analysing what the beams do to a voice in a real room needs the real
// microphones, all of them: the processed audio a command is saved as cannot
// say whether a loss is in one capsule, in the geometry the beams assume or in
// the beams. The standalone tools/capture_mics does it but has to stop the
// server to get the device, so this takes the same buffers the mic loop already
// reads.
//
// It is a file on the Echo, not a setting: create the request and the next
// seconds of raw audio are written, then the request is removed.
//
//	echo 20 > /data/local/etc/echomuse/capture.req
//
// writes 20 seconds (1-60, default 20) of interleaved S24_3LE, 9 channels,
// 16kHz, to /data/local/tmp/capture.raw: the layout capture_mics and
// tools/bf_design -raw read. It records speech, so nothing here persists it
// beyond that file and nothing sends it anywhere.

import (
	"log"
	"os"
	"strconv"
	"strings"
	"time"
)

var (
	captureReqPath = "/data/local/etc/echomuse/capture.req"
	captureOutPath = "/data/local/tmp/capture.raw"
)

const (
	captureBytesPerSecond = 16000 * 9 * 3
	captureMaxSeconds     = 60
	captureDefaultSeconds = 20
	capturePoll           = time.Second
)

type rawCapture struct {
	f     *os.File
	left  int
	total int
	next  time.Time // when to look for a request again
}

// feed takes one raw buffer. With no request it costs a stat a second.
func (c *rawCapture) feed(raw []byte, now time.Time) {
	if c.f == nil {
		if now.Before(c.next) {
			return
		}
		c.next = now.Add(capturePoll)
		if !c.begin() {
			return
		}
	}
	n := min(len(raw), c.left)
	if _, err := c.f.Write(raw[:n]); err != nil {
		log.Printf("[capture] write failed: %v", err)
		c.finish(false)
		return
	}
	c.left -= n
	if c.left <= 0 {
		c.finish(true)
	}
}

// begin reads a request and opens the output; false if there is none.
func (c *rawCapture) begin() bool {
	b, err := os.ReadFile(captureReqPath)
	if err != nil {
		return false
	}
	secs := captureDefaultSeconds
	if v, err := strconv.Atoi(strings.TrimSpace(string(b))); err == nil && v >= 1 {
		secs = min(v, captureMaxSeconds)
	}
	f, err := os.Create(captureOutPath)
	if err != nil {
		log.Printf("[capture] cannot open %s: %v", captureOutPath, err)
		os.Remove(captureReqPath)
		return false
	}
	c.f, c.left, c.total = f, secs*captureBytesPerSecond, secs*captureBytesPerSecond
	log.Printf("[capture] recording %ds of raw 9-channel audio to %s: speak now", secs, captureOutPath)
	return true
}

func (c *rawCapture) finish(ok bool) {
	if c.f != nil {
		c.f.Close()
	}
	c.f = nil
	os.Remove(captureReqPath)
	if ok {
		log.Printf("[capture] done: %s (%d bytes)", captureOutPath, c.total)
	}
}
