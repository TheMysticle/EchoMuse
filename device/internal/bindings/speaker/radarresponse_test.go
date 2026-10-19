package speaker

import (
	"encoding/binary"
	"math"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/outchain"
)

// responseLevel did nothing on Radar for a while: the boost stood down
// whenever the chain took the volume, which Radar always does. These drive
// the same pieces the speaker loop does (the response gains, the float
// chain entry point, the quantiser) over a volume-taking chain and check the
// boost reaches the output.

func radarVoiceRMS(t *testing.T, level int, db float64) float64 {
	t.Helper()
	const frames = 2048
	vol := VolumeGain(level)
	chain := outchain.NewForBoard(48000, "radar", nil)
	chain.SetVolumeGain(vol)
	chain.SetActive(true)
	if !chain.TakesVolume() {
		t.Fatal("a Radar chain must take the volume")
	}
	volume := settledVolume(level)
	response := settledResponse(db, volume)

	var out []byte
	for p := 0; p < 12; p++ {
		voice := make([]byte, frames*4)
		for i := 0; i < frames; i++ {
			x := int16(3000 * math.Sin(2*math.Pi*1000*float64(p*frames+i)/48000))
			binary.LittleEndian.PutUint16(voice[i*4:], uint16(x))
			binary.LittleEndian.PutUint16(voice[i*4+2:], uint16(x))
		}
		period := response.begin(volume, frames, volume.targetGain())
		gains := make([]float64, frames)
		period.fillGains(gains)
		var mixer Mixer
		mixer.SetGainImmediate(unityGain)
		wide := mixer.MixResponse(make([]float64, frames*2), voice, nil, unityGain, gains)
		chain.ProcessFloat(wide, gains)
		if !chain.TookVolume() {
			t.Fatal("the chain did not take the volume this period")
		}
		out = voice
		volume.settleFloat(wide, out)
		period.finish(gains)
	}
	var sum float64
	n := len(out) / 4
	for i := 0; i < n; i++ {
		v := float64(int16(binary.LittleEndian.Uint16(out[i*4:])))
		sum += v * v
	}
	return math.Sqrt(sum / float64(n))
}

func TestResponseLevelIsAudibleOnRadar(t *testing.T) {
	checked := 0
	for _, level := range []int{20, 40, 60} {
		vol := VolumeGain(level)
		want := cappedResponseGain(gainFromDB(6), vol)
		if want <= 1 {
			continue // capped to unity at this volume: documented behaviour
		}
		checked++
		low, med := radarVoiceRMS(t, level, 0), radarVoiceRMS(t, level, 6)
		if got := med / low; math.Abs(got-want) > 0.05*want {
			t.Errorf("level %d: Medium is %.3fx Low, want %.3fx", level, got, want)
		}
	}
	if checked == 0 {
		t.Fatal("no volume left room for a boost: the test checked nothing")
	}
}
