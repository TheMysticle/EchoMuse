package spotify

import "sync"

// ring is the byte buffer between the engine's audio socket and the
// speaker's period pull. The writer BLOCKS when it is full: that is the
// backpressure that paces librespot's decoder to the DAC, exactly as the
// sink's contract assumes.
//
// It is small on purpose. Whatever is in it plays after a pause, so its size
// is the tail you hear after pressing pause; the network buffering that
// protects against a stall lives inside librespot, not here.
type ring struct {
	mu     sync.Mutex
	space  *sync.Cond
	buf    []byte
	r, n   int
	closed bool
	primed bool // a whole period has been available since the last gap
}

func newRing(size int) *ring {
	g := &ring{buf: make([]byte, size)}
	g.space = sync.NewCond(&g.mu)
	return g
}

// write copies all of p in, waiting for room. It returns false once the ring
// is closed, with whatever was written already queued.
func (g *ring) write(p []byte) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	for len(p) > 0 {
		for g.n == len(g.buf) && !g.closed {
			g.space.Wait()
		}
		if g.closed {
			return false
		}
		w := (g.r + g.n) % len(g.buf)
		c := min(len(p), len(g.buf)-g.n, len(g.buf)-w)
		copy(g.buf[w:w+c], p[:c])
		g.n += c
		p = p[c:]
	}
	return true
}

// readPeriod copies exactly len(out) bytes out, or nothing at all.
//
// THIS IS THE RULE A PARTIAL READ BROKE. Fill used to take whatever was
// queued, zero-pad the rest of the period and report success — splicing
// silence into the middle of the waveform, which is a click. And it was
// invisible to every instrument: the period was delivered, on time, full
// length, so nothing starved, the ALSA buffer stayed healthy and the DAC
// never ran dry. Four counters in a row reported nothing while the audio
// was plainly glitching (Radar, 2026-10-11).
//
// Declining instead leaves those bytes queued, so the waveform resumes
// intact rather than with a hole punched in it. The cost is that the gap is
// a whole period instead of part of one — which is why `prime` exists: after
// a decline the ring must refill to `prime` before it is read again, so a
// brief jitter costs one gap rather than a run of them as the buffer
// oscillates around a period boundary.
//
// A decline is a starve and the caller counts it, which is what finally
// makes this measurable.
func (g *ring) readPeriod(out []byte, prime int) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	if !g.primed {
		if g.n < prime {
			return false
		}
		g.primed = true
	}
	if g.n < len(out) {
		g.primed = false
		return false
	}
	got := 0
	for got < len(out) {
		c := min(len(out)-got, g.n, len(g.buf)-g.r)
		copy(out[got:], g.buf[g.r:g.r+c])
		g.r = (g.r + c) % len(g.buf)
		g.n -= c
		got += c
	}
	g.space.Broadcast()
	return true
}

// read copies up to len(out) bytes out without waiting and returns how many.
func (g *ring) read(out []byte) int {
	g.mu.Lock()
	defer g.mu.Unlock()
	got := 0
	for got < len(out) && g.n > 0 {
		c := min(len(out)-got, g.n, len(g.buf)-g.r)
		copy(out[got:], g.buf[g.r:g.r+c])
		g.r = (g.r + c) % len(g.buf)
		g.n -= c
		got += c
	}
	if got > 0 {
		g.space.Broadcast()
	}
	return got
}

func (g *ring) len() int {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.n
}

// flush discards everything queued, for a track change or a stop.
func (g *ring) flush() {
	g.mu.Lock()
	g.r, g.n, g.primed = 0, 0, false
	g.space.Broadcast()
	g.mu.Unlock()
}

// close wakes a blocked writer, which then returns false.
func (g *ring) close() {
	g.mu.Lock()
	g.closed = true
	g.space.Broadcast()
	g.mu.Unlock()
}

// reopen readies a closed ring for a new engine process.
func (g *ring) reopen() {
	g.mu.Lock()
	g.closed = false
	g.r, g.n, g.primed = 0, 0, false
	g.mu.Unlock()
}
