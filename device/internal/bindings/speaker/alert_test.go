package speaker

import (
	"encoding/binary"
	"math"
	"sync/atomic"
	"testing"
	"time"
)

func TestAlertLoopsWithoutMutatingSharedSilence(t *testing.T) {
	var a AlertAudio
	sound := []byte{0x10, 0x27}
	a.Set(sound, nil, 0.5, 0)
	silence := make([]byte, 16)
	out := a.Mix(silence, false, unityGain)
	for _, v := range silence {
		if v != 0 {
			t.Fatal("shared silence modified")
		}
	}
	for i := 0; i < len(out); i += 2 {
		if int16(binary.LittleEndian.Uint16(out[i:])) != 5000 {
			t.Fatal("did not loop")
		}
	}
	a.Stop()
	if a.Active() {
		t.Fatal("stop failed")
	}
}

func TestAlertVoiceTurnMix(t *testing.T) {
	for _, tc := range []struct {
		name   string
		voice  bool
		target int32
		base   int16
		want   float64
	}{
		{"ringing over music", false, unityGain, 2000, 400 + 5000},
		{"listening without music", false, DuckGain(-18), 0, 5000 * float64(DuckGain(-18)) / float64(unityGain)},
		{"listening with music", false, DuckGain(-18), 2000, 400 + 5000*float64(DuckGain(-18))/float64(unityGain)},
		{"reply stays full volume without double duck", true, DuckGain(-18), 2000, 2000 + 5000*float64(DuckGain(-18))/float64(unityGain)},
		{"unsolicited reply still ducks alert", true, unityGain, 2000, 2000 + 5000*float64(unityGain*15/100)/float64(unityGain)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var a AlertAudio
			a.Set([]byte{0x10, 0x27}, nil, 0.5, 0)
			base := make([]byte, 8192)
			for i := 0; i < len(base); i += 2 {
				binary.LittleEndian.PutUint16(base[i:], uint16(tc.base))
			}
			a.Mix(base, tc.voice, tc.target) // settle the sample-interpolated ramp
			out := a.Mix(base, tc.voice, tc.target)
			for i := 0; i < len(out); i += 2 {
				got := int16(binary.LittleEndian.Uint16(out[i:]))
				if math.Abs(float64(got)-tc.want) > 1 {
					t.Fatalf("sample %d: got %d, want %.2f", i/2, got, tc.want)
				}
			}
			if got := int16(binary.LittleEndian.Uint16(base)); got != tc.base {
				t.Fatal("modified shared input")
			}
		})
	}
}

func TestAlertDuckRampsAndRestores(t *testing.T) {
	var a AlertAudio
	a.Set([]byte{0x10, 0x27}, nil, 0.5, 0)
	base := make([]byte, 8192)
	for _, target := range []int32{DuckGain(-18), unityGain} {
		out := a.Mix(base, false, target)
		first := int16(binary.LittleEndian.Uint16(out))
		last := int16(binary.LittleEndian.Uint16(out[len(out)-2:]))
		if target == unityGain && (first >= last || first > 650 || last < 4900) {
			t.Fatalf("did not fade back up: %d -> %d", first, last)
		}
		if target != unityGain && (first <= last || first != 5000 || last > 650) {
			t.Fatalf("did not fade down: %d -> %d", first, last)
		}
	}
	if got := int16(binary.LittleEndian.Uint16(a.Mix(base, false, unityGain))); got != 5000 {
		t.Fatalf("did not restore configured alert volume: %d", got)
	}
}

func TestAlertOnlyLocalWakeAndRelease(t *testing.T) {
	for _, confirmed := range []bool{false, true} {
		var target atomic.Int32
		target.Store(unityGain)
		d := NewLocalDuck(func(db float64) { target.Store(DuckGain(db)) }, time.Hour)
		var a AlertAudio
		a.Set([]byte{0x10, 0x27}, nil, 0.5, 0)
		d.Start(-18) // no streamed music or response exists
		base := make([]byte, 8192)
		a.Mix(base, false, target.Load())
		if got := int16(binary.LittleEndian.Uint16(a.Mix(base, false, target.Load()))); got > 650 {
			t.Fatalf("wake did not duck the alert: %d", got)
		}
		if confirmed {
			d.Confirm()
			d.Cancel()
			if target.Load() == unityGain {
				t.Fatal("session close released an ongoing confirmed turn")
			}
			d.Reset() // control link lost during an ongoing turn
		} else {
			d.Cancel() // ceded wake or ended without controller confirmation
		}
		a.Mix(base, false, target.Load())
		if got := int16(binary.LittleEndian.Uint16(a.Mix(base, false, target.Load()))); got != 5000 {
			t.Fatalf("release left alert quiet: %d", got)
		}
		if !a.Active() {
			t.Fatal("ducking dismissed the alert")
		}
	}
}

func TestReminderChimePrecedesEverySpeechAndStops(t *testing.T) {
	for _, repeats := range []int{0, 1, 3} {
		var a AlertAudio
		a.SetReminder([]byte{100, 0}, []byte{200, 0, 250, 0}, 1, repeats)
		for pass := 0; pass <= repeats; pass++ {
			out := a.Mix(make([]byte, 12), false, unityGain)
			for frame, want := range []uint16{100, 200, 250} {
				if got := binary.LittleEndian.Uint16(out[frame*4:]); got != want {
					t.Fatalf("pass %d frame %d: %d != %d", pass, frame, got, want)
				}
			}
		}
		// Finishing releases music attenuation and does not restart the chime.
		// The mix comes back up over a few hundred samples, not in one step.
		base := make([]byte, 4*1000)
		for i := 0; i < len(base); i += 2 {
			binary.LittleEndian.PutUint16(base[i:], 1000)
		}
		out := a.Mix(base, false, unityGain)
		first := binary.LittleEndian.Uint16(out)
		last := binary.LittleEndian.Uint16(out[len(out)-4:])
		if a.Active() || first >= 1000 || last != 1000 {
			t.Fatalf("reminder did not finish cleanly: active=%v first=%d last=%d", a.Active(), first, last)
		}
	}
}

func TestReminderStopAndMissingSpeech(t *testing.T) {
	var a AlertAudio
	a.SetReminder([]byte{100, 0}, nil, 1, 2)
	out := a.Mix(make([]byte, 16), false, unityGain)
	for n, want := range []uint16{100, 100, 100, 0} {
		if binary.LittleEndian.Uint16(out[n*4:]) != want {
			t.Fatal("wrong sound-only fallback")
		}
	}
	a.SetReminder([]byte{100, 0}, []byte{200, 0}, 1, 10)
	a.Stop()
	if a.Active() || binary.LittleEndian.Uint16(a.Mix(make([]byte, 4), false, unityGain)) != 0 {
		t.Fatal("dismissal left queued repeats")
	}
}

// The rest of the mix is lowered under an alert and restored after it, and
// neither edge may be a step: a 1 to 0.2 jump on music is a click.
func TestAlertAttenuatesTheMixWithoutAStepAtEitherEdge(t *testing.T) {
	var a AlertAudio
	level := func(buf []byte) []float64 {
		out := make([]float64, len(buf)/4)
		for i := range out {
			out[i] = float64(int16(binary.LittleEndian.Uint16(buf[i*4:])))
		}
		return out
	}
	music := make([]byte, 4*4800)
	for i := 0; i < len(music); i += 2 {
		binary.LittleEndian.PutUint16(music[i:], uint16(int16(10000)))
	}
	// A silent alert, so only the gain on the music is visible.
	a.Set(make([]byte, 96000), nil, 0.5, 0)
	maxStep := 10000*baseRamp + 1
	check := func(name string, got []float64, prev float64) float64 {
		for i, v := range got {
			if math.Abs(v-prev) > maxStep {
				t.Fatalf("%s: step of %.1f at sample %d, want at most %.1f", name, v-prev, i, maxStep)
			}
			prev = v
		}
		return prev
	}
	down := level(a.Mix(music, false, unityGain))
	if down[0] < 9970 {
		t.Fatalf("first sample %.1f: the alert must start from the unattenuated mix", down[0])
	}
	last := check("start", down, 10000)
	if math.Abs(last-2000) > 1 {
		t.Fatalf("settled at %.1f, want 2000", last)
	}
	a.Stop()
	up := level(a.Mix(music, false, unityGain))
	last = check("end", up, 2000)
	if math.Abs(last-10000) > 1 {
		t.Fatalf("restored to %.1f, want 10000", last)
	}
	if got := a.Mix(music, false, unityGain); &got[0] != &music[0] {
		t.Error("once restored, an idle alert must return the mix untouched")
	}
}
