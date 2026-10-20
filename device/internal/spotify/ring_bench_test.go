package spotify

import "testing"

// The ring sits between the engine and the speaker on every Echo that plays
// Spotify, group or not. These hold the cost of the group leader's hooks (the
// flush counter and the reader-watch check in write) to nothing when no
// leader is reading.

// A period as the speaker pulls it: 20 ms of 48 kHz stereo S16.
const benchPeriod = 960 * 4

func BenchmarkRingWriteThenRead(b *testing.B) {
	g := newRing(48000)
	in, out := make([]byte, benchPeriod), make([]byte, benchPeriod)
	b.ReportAllocs()
	b.SetBytes(benchPeriod)
	for i := 0; i < b.N; i++ {
		g.write(in)
		g.readPeriod(out, 0)
	}
}

func BenchmarkRingFlush(b *testing.B) {
	g := newRing(48000)
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		g.flush()
	}
}

func TestRingWriteAndReadAllocateNothing(t *testing.T) {
	g := newRing(48000)
	in, out := make([]byte, benchPeriod), make([]byte, benchPeriod)
	if n := testing.AllocsPerRun(1000, func() {
		g.write(in)
		g.readPeriod(out, 0)
	}); n != 0 {
		t.Errorf("write+read allocates %v times per period", n)
	}
}

// With no leader reading, write must not touch the notify channel at all:
// nothing is queued on it and the watch count stays zero.
func TestRingWriteLeavesTheNotifyChannelAloneWhenNobodyWatches(t *testing.T) {
	g := newRing(48000)
	for i := 0; i < 100; i++ {
		g.write(make([]byte, benchPeriod))
		g.readPeriod(make([]byte, benchPeriod), 0)
	}
	if len(g.notify) != 0 || g.watch.Load() != 0 {
		t.Errorf("notify has %d queued, watch is %d", len(g.notify), g.watch.Load())
	}
}
