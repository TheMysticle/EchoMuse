package outchain

import (
	"math"
	"path/filepath"
	"testing"

	"github.com/wilbowes/EchoMuse/pkg/board"
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
func benchBoard(b *testing.B, rt *RadarTuning, p Params) {
	c := NewWithTuning(48000, DefaultGuardBand, board.BiscuitVoicing(), rt)
	c.SetActive(true)
	c.SetParams(p)
	// Mid-scale rather than unity so a curve is genuinely loaded.
	c.SetVolume(115)
	wide := make([]float64, 2048*2)
	for i := 0; i < 2048; i++ {
		x := 20000 * math.Sin(2*math.Pi*80*float64(i)/48000)
		wide[i*2], wide[i*2+1] = x, x
	}
	out := make([]byte, 2048*4)
	b.SetBytes(int64(len(out)))
	for i := 0; i < b.N; i++ {
		c.ProcessWide(wide, out)
	}
}

func BenchmarkBoardBiscuitDefaults(b *testing.B) {
	benchBoard(b, nil, DefaultParams())
}

func BenchmarkBoardRadarStockCurve(b *testing.B) {
	// How the fleet actually runs a Radar: its own tuning, the FIR curve on.
	rt, err := LoadRadarTuning(filepath.Join("testdata", "radar_tuning"))
	if err != nil {
		b.Fatal(err)
	}
	benchBoard(b, rt, DefaultParams())
}
