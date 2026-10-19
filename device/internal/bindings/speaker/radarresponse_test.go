package speaker

import (
	"encoding/binary"
	"math"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/outchain"
)

func radarChain(volume float64) *outchain.Chain {
	c := outchain.NewForBoard(48000, "radar")
	c.SetVolumeGain(volume)
	c.SetActive(true)
	return c
}

func sinePeriod(frames int, amp float64) []float64 {
	w := make([]float64, frames*2)
	for i := 0; i < frames; i++ {
		x := math.Round(amp * math.Sin(2*math.Pi*1000*float64(i)/48000))
		w[i*2], w[i*2+1] = x, x
	}
	return w
}

func toS16(w []float64) []byte {
	b := make([]byte, len(w)*2)
	for i, x := range w {
		binary.LittleEndian.PutUint16(b[i*2:], uint16(int16(x)))
	}
	return b
}

func rms(b []byte) float64 {
	var sum float64
	n := len(b) / 2
	for i := 0; i < n; i++ {
		v := float64(int16(binary.LittleEndian.Uint16(b[i*2:])))
		sum += v * v
	}
	return math.Sqrt(sum / float64(n))
}

// With nothing to scale by, the float entry point on a volume-taking chain
// must land where the S16 path does, so a response at 0dB is unchanged.
func TestRadarFloatPathMatchesS16Path(t *testing.T) {
	const frames, vol = 1024, 0.2
	in := sinePeriod(frames, 6000)
	a, b := radarChain(vol), radarChain(vol)
	s16 := toS16(in)
	a.Process(s16)
	wide := append([]float64(nil), in...)
	b.ProcessFloat(wide, nil)
	got := make([]byte, len(s16))
	quantizeFloat(wide, got)
	for i := range s16 {
		if s16[i] != got[i] {
			t.Fatalf("byte %d: S16 path %d, float path %d", i, s16[i], got[i])
		}
	}
	if !b.TookVolume() {
		t.Fatal("chain did not take the volume")
	}
}

// The reported bug: on Radar the boost did nothing. A +6dB voice, capped
// against the volume as the speaker caps it, must come out louder than 0dB.
func TestRadarResponseBoostIsAudible(t *testing.T) {
	const frames = 1024
	for _, level := range []int{20, 40, 60} {
		vol := VolumeGain(level)
		low, high := radarChain(vol), radarChain(vol)
		g := cappedResponseGain(gainFromDB(6), vol)
		a := sinePeriod(frames, 4000)
		b := sinePeriod(frames, 4000*g)
		// Settle the filters and compressors over a few periods.
		var outA, outB []byte
		for i := 0; i < 8; i++ {
			wa := append([]float64(nil), a...)
			wb := append([]float64(nil), b...)
			low.ProcessFloat(wa, nil)
			high.ProcessFloat(wb, nil)
			outA, outB = make([]byte, frames*4), make([]byte, frames*4)
			quantizeFloat(wa, outA)
			quantizeFloat(wb, outB)
		}
		if g <= 1 {
			continue // capped to unity at this volume: documented behaviour
		}
		if rms(outB) <= rms(outA) {
			t.Errorf("level %d: boosted rms %.1f not above plain %.1f", level, rms(outB), rms(outA))
		}
	}
}

func TestQuantizeFloatClamps(t *testing.T) {
	out := make([]byte, 8)
	quantizeFloat([]float64{40000, -40000, 1.4, -1.6}, out)
	want := []int16{math.MaxInt16, math.MinInt16, 1, -2}
	for i, w := range want {
		if got := int16(binary.LittleEndian.Uint16(out[i*2:])); got != w {
			t.Errorf("sample %d: got %d want %d", i, got, w)
		}
	}
}
