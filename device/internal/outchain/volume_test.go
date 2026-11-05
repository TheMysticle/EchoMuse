package outchain

import (
	"encoding/binary"
	"github.com/wilbowes/EchoMuse/pkg/board"
	"math"
	"testing"
)

func tone(frames int, hz, amp float64) []byte {
	buf := make([]byte, frames*4)
	for i := 0; i < frames; i++ {
		s := int16(amp * math.Sin(2*math.Pi*hz*float64(i)/48000))
		binary.LittleEndian.PutUint16(buf[i*4:], uint16(s))
		binary.LittleEndian.PutUint16(buf[i*4+2:], uint16(s))
	}
	return buf
}

func rmsOf(buf []byte) float64 {
	var sum float64
	n := len(buf) / 4
	for i := 0; i < n; i++ {
		v := float64(int16(binary.LittleEndian.Uint16(buf[i*4:])))
		sum += v * v
	}
	return math.Sqrt(sum / float64(n))
}

// The volume is ahead of the MBCL (the stock profile's order): its
// compressors see the level the listener hears, so 20dB of volume moves the
// compressed bass by much less than 20dB — and at a level nobody listens at,
// they are not holding the bass down.
func TestRadarVolumeAheadOfMBCL(t *testing.T) {
	run := func(gain float64, level int) float64 {
		c := NewWithTuning(48000, DefaultGuardBand, board.BiscuitVoicing(), radarTuningForTest(t))
		c.SetActive(true)
		c.SetVolume(level)
		var out float64
		for k := 0; k < 12; k++ {
			wide := make([]float64, 2048*2)
			for i := 0; i < 2048; i++ {
				x := gain * 20000 * math.Sin(2*math.Pi*100*float64(i)/48000)
				wide[i*2], wide[i*2+1] = x, x
			}
			buf := make([]byte, 2048*4)
			c.ProcessWide(wide, buf)
			out = rmsOf(buf)
		}
		return out
	}
	full, quiet := run(1, 127), run(0.1, 87)
	drop := 20 * math.Log10(full/quiet)
	if drop >= 19 {
		t.Errorf("20dB of volume dropped the compressed bass by %.1fdB; want well under 20", drop)
	}
	t.Logf("20dB of volume -> %.1fdB at the output", drop)
}

// The FIR band follows the device level by stock's mixer law.
func TestLevelGainIsStocksMixerLaw(t *testing.T) {
	if levelGain(0) != 0 || levelGain(127) != 1 || levelGain(200) != 1 {
		t.Error("edges")
	}
	if g := levelGain(87); math.Abs(g-0.1) > 1e-12 {
		t.Errorf("level 87 = %g, want 0.1 (-20dB)", g)
	}
}

// Every stock volume value survives the trip to a device level and back,
// and the levels are stock's (value + 27 from 11 up, 127 at 100).
func TestStockVolumeRoundTrips(t *testing.T) {
	for v := 0; v <= 100; v++ {
		if got := StockVolumeValue(StockVolumeLevel(v)); got != v {
			t.Errorf("value %d -> level %d -> value %d", v, StockVolumeLevel(v), got)
		}
	}
	if StockVolumeLevel(30) != 57 || StockVolumeLevel(100) != 127 || StockVolumeLevel(5) != 20 {
		t.Errorf("levels 30/100/5 = %d/%d/%d, want 57/127/20",
			StockVolumeLevel(30), StockVolumeLevel(100), StockVolumeLevel(5))
	}
}
