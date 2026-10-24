package speaker

import (
	"encoding/binary"
	"math"
	"sync"
)

// AlertAudio is independent of streamed voice/music lifetimes and flushes.
// PCM is mono 48kHz. Mix runs before the AEC tap, after output volume.
// The scheduler only replaces it when the active occurrence changes.
type AlertAudio struct {
	mu            sync.Mutex
	sound, speech []byte
	pos           int
	remaining     int // -1 loops; a reminder plays a finite chime/speech sequence
	sequence      bool
	age           int64
	ramp          int64
	volume        float64
	scratch       []byte
	active        bool
	duck          Mixer
	// base is the gain on whatever else is in the mix (music, a reply). It
	// moves toward its target a little each sample: a step from 1 to 0.2 at
	// the first frame of an alert, or back at its last, is a click.
	base float64
}

// baseRamp is how far base moves per sample: the whole 0.2..1 swing in 10ms.
const baseRamp = 0.8 / 480

func (a *AlertAudio) Set(sound, speech []byte, volume float64, rampSeconds int64) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.setLocked(sound, speech, volume, rampSeconds)
}
func (a *AlertAudio) setLocked(sound, speech []byte, volume float64, rampSeconds int64) {
	a.sound = append([]byte(nil), sound...)
	a.speech = append([]byte(nil), speech...)
	a.pos = 0
	a.remaining = -1
	a.sequence = false
	a.age = 0
	a.ramp = rampSeconds * 48000
	if a.base == 0 {
		a.base = 1 // never played: start from the unattenuated mix
	}
	a.volume = volume
	a.duck.SetGainImmediate(unityGain)
	a.active = true
}

// SetReminder plays the chime before speech on every pass, with no volume ramp.
func (a *AlertAudio) SetReminder(sound, speech []byte, volume float64, repeats int) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.setLocked(sound, speech, volume, 0)
	a.sequence = true
	a.remaining = 1 + repeats
}
func (a *AlertAudio) Stop() {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.active = false
	a.sound = nil
	a.speech = nil
}
func (a *AlertAudio) Active() bool { a.mu.Lock(); defer a.mu.Unlock(); return a.active }

// SetSlices says how many mixes make one period of duck ramp (see Mixer).
func (a *AlertAudio) SetSlices(n int) {
	a.mu.Lock()
	a.duck.SetSlices(n)
	a.mu.Unlock()
}

func (a *AlertAudio) Mix(out []byte, voice bool, duckTarget int32) []byte {
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.active && a.base >= 1 {
		return out
	}
	if cap(a.scratch) < len(out) {
		a.scratch = make([]byte, len(out))
	}
	buf := a.scratch[:len(out)]
	copy(buf, out)
	// Follow the same turn target as music, including the listening phase.
	// Keep the reply-only attenuation for unsolicited TTS, but never multiply
	// the two ducks: the quieter target wins. Ramp just the alert contribution
	// so the assistant's voice remains untouched.
	if voice {
		duckTarget = min(duckTarget, unityGain*15/100)
	}
	start := a.duck.Gain()
	a.duck.stepGain(duckTarget)
	end := a.duck.Gain()
	frames := len(buf) / 4
	for off := 0; off+3 < len(buf); off += 4 {
		var sample float64
		if !a.active {
			// Stopped, still ramping the rest of the mix back up.
		} else if a.sequence {
			length := len(a.sound) + len(a.speech)
			if a.pos >= length {
				a.remaining--
				a.pos = 0
				if a.remaining <= 0 || length < 2 {
					a.active = false
				}
			}
			if a.active {
				clip, pos := a.sound, a.pos
				if pos >= len(clip) {
					pos -= len(clip)
					clip = a.speech
				}
				if pos+1 < len(clip) {
					sample = float64(int16(binary.LittleEndian.Uint16(clip[pos:])))
				}
				a.pos += 2
			}
		} else if a.pos+1 < len(a.speech) {
			sample = float64(int16(binary.LittleEndian.Uint16(a.speech[a.pos:])))
			a.pos += 2
		} else {
			if a.speech != nil {
				a.speech = nil
				a.pos = 0
			}
			if len(a.sound) >= 2 {
				if a.pos >= len(a.sound) {
					a.pos = 0
				}
				sample = float64(int16(binary.LittleEndian.Uint16(a.sound[a.pos:])))
				a.pos += 2
			} else {
				phase := a.age % 96000
				if phase < 48000 {
					sample = 6000 * math.Sin(2*math.Pi*880*float64(a.age)/48000)
				}
			}
		}
		gain := a.volume
		g := start + (end-start)*int32(off/4)/int32(frames)
		gain *= float64(g) / float64(unityGain)
		if a.ramp > 0 && a.age < a.ramp {
			gain *= 0.1 + 0.9*float64(a.age)/float64(a.ramp)
		}
		target := 0.2
		if !a.active || voice {
			target = 1
		}
		if a.base < target {
			a.base = math.Min(target, a.base+baseRamp)
		} else if a.base > target {
			a.base = math.Max(target, a.base-baseRamp)
		}
		baseGain := a.base
		for ch := 0; ch < 2; ch++ {
			p := off + ch*2
			v := float64(int16(binary.LittleEndian.Uint16(buf[p:])))*baseGain + sample*gain
			v = math.Max(-32768, math.Min(32767, v))
			binary.LittleEndian.PutUint16(buf[p:], uint16(int16(v)))
		}
		a.age++
	}
	return buf
}
