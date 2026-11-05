//go:build server

package speaker

import (
	"encoding/binary"
	"math"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/outchain"
	"github.com/wilbowes/EchoMuse/pkg/board"
)

// A low-latency Sendspin pull is lowLatencyPeriodFrames, not a whole period,
// and the stock-sound path must write exactly what it was given: a 256-frame
// pull written as 2048 frames is stale audio in the gap and a ring four times
// deeper than the low-latency path holds it to. Mixed lengths in any order,
// with the chain both active and not, and the silence after them too.
func TestStockPeriodIsAsLongAsItsAudio(t *testing.T) {
	for _, active := range []bool{false, true} {
		chain := outchain.NewFor(48000, outchain.DefaultGuardBand, board.BiscuitVoicing())
		chain.SetActive(active)
		p := &PcmSpeaker{
			chain:       chain,
			chainBuf:    make([]byte, periodBytes),
			stockWide:   make([]float64, periodSize*2),
			responseMix: make([]float64, periodSize*2),
			voiceFilter: outchain.NewVoiceFilter(48000, nil),
		}
		p.mixer.SetGainImmediate(unityGain)
		p.vol.cur = 1
		phase := 0
		for _, frames := range []int{periodSize, lowLatencyPeriodFrames, lowLatencyPeriodFrames, 512, periodSize, lowLatencyPeriodFrames} {
			music := make([]byte, frames*4)
			for i := 0; i < frames; i++ {
				v := int16(8000 * math.Sin(2*math.Pi*440*float64(phase)/48000))
				phase++
				binary.LittleEndian.PutUint16(music[i*4:], uint16(v))
				binary.LittleEndian.PutUint16(music[i*4+2:], uint16(v))
			}
			if out := p.stockPeriod(nil, music, nil, 1, unityGain, len(music)); len(out) != len(music) {
				t.Fatalf("active=%v: %d frames of music wrote %d", active, frames, len(out)/4)
			}
		}
		for i := 0; i < 8; i++ { // tails ringing out, then idle
			n := lowLatencyPeriodFrames * 4
			if out := p.stockPeriod(nil, nil, nil, 1, unityGain, n); len(out) != n {
				t.Fatalf("active=%v: silence of %d frames wrote %d", active, n/4, len(out)/4)
			}
		}
	}
}
