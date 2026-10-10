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
	g.r, g.n = 0, 0
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
	g.r, g.n = 0, 0
	g.mu.Unlock()
}
