// Package cpuprofile records a Go CPU profile of the running server on request.
//
// The server is one process whose threads are all named "server", so top -H
// can say THAT it is busy but not which goroutine is. A pprof profile names
// the functions. It is a file on the Echo, not a setting, in the style of the
// raw microphone capture (internal/client/capture.go): create the request and
// the next seconds are profiled, then the request is removed.
//
//	echo 30 > /data/local/etc/echomuse/cpuprofile.req
//
// writes 30 seconds (1-120, default 30) to /data/local/tmp/cpu.pprof. With no
// request it costs one stat a second. Nothing here is sent anywhere.
package cpuprofile

import (
	"context"
	"log"
	"os"
	"runtime/pprof"
	"strconv"
	"strings"
	"time"
)

var (
	reqPath = "/data/local/etc/echomuse/cpuprofile.req"
	outPath = "/data/local/tmp/cpu.pprof"
)

const (
	maxSeconds     = 120
	defaultSeconds = 30
	poll           = time.Second
)

// Watch looks for a request once a second until ctx ends.
func Watch(ctx context.Context) {
	t := time.NewTicker(poll)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
		if secs, ok := request(); ok {
			record(ctx, time.Duration(secs)*time.Second)
		}
	}
}

// request reads and consumes a request; ok is false if there is none.
func request() (int, bool) {
	if _, err := os.Stat(reqPath); err != nil {
		return 0, false
	}
	secs := defaultSeconds
	if b, err := os.ReadFile(reqPath); err == nil {
		if v, err := strconv.Atoi(strings.TrimSpace(string(b))); err == nil && v >= 1 {
			secs = min(v, maxSeconds)
		}
	}
	// Removed first, so a profile that fails cannot be retried every second.
	os.Remove(reqPath)
	return secs, true
}

func record(ctx context.Context, d time.Duration) {
	f, err := os.Create(outPath)
	if err != nil {
		log.Printf("[cpuprofile] cannot open %s: %v", outPath, err)
		return
	}
	defer f.Close()
	if err := pprof.StartCPUProfile(f); err != nil {
		log.Printf("[cpuprofile] cannot start: %v", err)
		return
	}
	log.Printf("[cpuprofile] profiling for %s into %s", d, outPath)
	select {
	case <-time.After(d):
	case <-ctx.Done():
	}
	pprof.StopCPUProfile()
	log.Printf("[cpuprofile] done: %s", outPath)
}
