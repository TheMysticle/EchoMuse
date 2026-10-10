package outchain

import (
	"math"
	"testing"
)

// Radar's chain cost has never been measured, on hardware or here, and it is
// substantially more work than the one that WAS measured.
//
// The published figure — 1.9ms per 42.7ms period at defaults, 2.6ms worst
// case — is biscuit's, taken on VVV's A53 (device/CLAUDE.md, internal/
// outchain). Radar runs something else entirely since 2026-10-10: four stock
// band compressors with per-block power detection and two-stage smoothing,
// four band limiters plus a full-band one, Butterworth crossovers, and
// optionally stock's own FIR curve ahead of the eight bands. Quoting
// biscuit's number for a Radar is quoting a measurement of different code.
//
// These benchmarks cannot give an absolute figure — the host is x86 and the
// device is a 1.3GHz A53 — but the RATIO between the two boards transfers,
// and biscuit's side is pinned to a real hardware measurement. Run:
//
//	go test ./internal/outchain/ -run XXX -bench 'Board' -benchtime 200x
//
// What the ratio is for: the write loop has one period, 42.7ms, to filter
// and hand over the next buffer. Whatever the chain costs is spent alongside
// the mic pipeline (~18-20% of a core) and, with owwOnDevice on, wake word
// inference (~38%). A chain several times biscuit's turns a comfortable
// budget into a tight one, and a late write is an underrun — which is heard
// as a click.
func benchBoard(b *testing.B, boardID string, p Params) {
	c := NewForBoard(48000, boardID)
	c.SetActive(true)
	c.SetParams(p)
	if c.TakesVolume() {
		// Radar applies the volume itself, and the level selects the stock
		// curve. Mid-scale rather than unity so a curve is genuinely loaded.
		c.SetVolumeGain(0.5)
	}
	mono := make([]int16, 2048)
	for i := range mono {
		mono[i] = int16(20000 * math.Sin(2*math.Pi*80*float64(i)/48000))
	}
	src := stereo(mono)
	buf := make([]byte, len(src))
	b.SetBytes(int64(len(src)))
	for i := 0; i < b.N; i++ {
		copy(buf, src)
		c.Process(buf)
	}
}

func BenchmarkBoardBiscuitDefaults(b *testing.B) {
	benchBoard(b, "biscuit", DefaultParams())
}

func BenchmarkBoardRadarDefaults(b *testing.B) {
	benchBoard(b, "radar", DefaultParams())
}

func BenchmarkBoardRadarStockCurve(b *testing.B) {
	// How the fleet actually runs a Radar: stock's FIR curve on.
	p := DefaultParams()
	p.StockCurve = true
	benchBoard(b, "radar", p)
}
