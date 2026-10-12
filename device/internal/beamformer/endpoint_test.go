package beamformer

import (
	"encoding/binary"
	"math"
	"math/rand"
	"testing"
)

// A scene, one sample at a time. The TV is a steady noise of tv rms (with the
// slow level wobble of dialogue); the talker is speech-shaped bursts: syllables
// of 150-250ms with 50-100ms between, and phrases separated by gaps.
type epScene struct {
	rng    *rand.Rand
	t      int // samples
	tv     float64
	room   float64
	talker float64
	// speaking(t) says whether the talker is on at sample t.
	speaking func(t int) bool
	lp       float64 // one-pole low-pass state for the TV's slow shape
}

func (s *epScene) next(n int) []byte {
	out := make([]byte, n*2)
	for i := 0; i < n; i++ {
		// TV: pink-ish noise with a slow wobble.
		w := s.rng.NormFloat64()
		s.lp = 0.9*s.lp + 0.1*w
		tv := s.tv * (0.8*s.lp*3 + 0.6*w) * (1 + 0.25*math.Sin(2*math.Pi*0.7*float64(s.t)/16000))
		v := tv + s.rng.NormFloat64()*s.room
		if s.speaking != nil && s.speaking(s.t) {
			// Voiced: harmonics of 120Hz under a syllable envelope.
			tt := float64(s.t) / 16000
			env := 0.5 + 0.5*math.Sin(2*math.Pi*4*tt)
			var h float64
			for k := 1; k <= 12; k++ {
				h += math.Sin(2*math.Pi*120*float64(k)*tt+float64(k)) / float64(k)
			}
			v += s.talker * env * h * 0.9
		}
		q := int16(math.Max(-32768, math.Min(32767, v)))
		binary.LittleEndian.PutUint16(out[i*2:], uint16(q))
		s.t++
	}
	return out
}

func newScene(tv, talker float64, start, stop int) *epScene {
	return &epScene{rng: rand.New(rand.NewSource(3)), tv: tv, room: 8, talker: talker,
		speaking: func(t int) bool { return t >= start*16 && t < stop*16 }}
}

// run feeds the scene to the endpointer in 80ms chunks and returns when, in ms,
// the session ended (-1 if it did not) and the time the talker stopped.
func runEP(t *testing.T, sc *epScene, ep *Endpointer, warmMs, sessionAtMs, totalMs int) (endedAt int) {
	t.Helper()
	endedAt = -1
	for ms := 0; ms < totalMs; ms += 80 {
		ep.Push(sc.next(1280))
		now := ms + 80
		if now == sessionAtMs {
			ep.StartSession(0)
		}
		if endedAt < 0 && ep.Ended() {
			endedAt = now
		}
	}
	return endedAt
}

// With the TV on the whole time and a command spoken over it, the command is
// over about a second after the talker stops, not when the TV does.
func TestEndsAboutASecondAfterTheTalkerStopsOverASteadyTV(t *testing.T) {
	// 8s of TV alone (the floor), the wake at 8s, speech 8.4s-11s, the TV on to 20s.
	sc := newScene(900, 4000, 8400, 11000)
	ep := NewEndpointer()
	end := runEP(t, sc, ep, 0, 8000, 20000)
	if end < 0 {
		t.Fatal("never ended with the TV on")
	}
	if end < 11000+800 || end > 11000+1600 {
		t.Errorf("ended at %dms; the talker stopped at 11000ms, expected within 0.8-1.6s after", end)
	}
}

// The same in a quiet room.
func TestEndsInAQuietRoomToo(t *testing.T) {
	sc := newScene(0, 3000, 8400, 10000)
	end := runEP(t, sc, NewEndpointer(), 0, 8000, 16000)
	if end < 10000+800 || end > 10000+1600 {
		t.Errorf("ended at %dms, talker stopped at 10000ms", end)
	}
}

// A TV alone is never a command.
func TestATVAloneIsNeverSpeech(t *testing.T) {
	sc := newScene(1500, 0, 1<<30, 1<<30)
	ep := NewEndpointer()
	for ms := 0; ms < 30000; ms += 80 {
		ep.Push(sc.next(1280))
		if ms == 8000 {
			ep.StartSession(0)
		}
		if ep.Heard() {
			t.Fatalf("heard speech at %dms in a steady TV", ms)
		}
	}
}

// A pause inside a sentence is not the end of it.
func TestAPauseInTheSentenceDoesNotEndIt(t *testing.T) {
	sc := newScene(900, 4000, 8400, 1<<30)
	sc.speaking = func(t int) bool {
		ms := t / 16
		return (ms >= 8400 && ms < 10000) || (ms >= 10600 && ms < 13000) // a 0.6s pause
	}
	end := runEP(t, sc, NewEndpointer(), 0, 8000, 20000)
	if end >= 0 && end < 13000 {
		t.Errorf("ended at %dms, inside the sentence (pause 10.0-10.6s)", end)
	}
	if end < 0 {
		t.Error("never ended after the sentence")
	}
}

// A long sentence must not become the floor it is judged against: 12 seconds of
// speech with the usual gaps, then silence, still ends.
func TestALongSentenceDoesNotRaiseTheFloor(t *testing.T) {
	sc := newScene(900, 4000, 0, 1<<30)
	sc.speaking = func(t int) bool {
		ms := t / 16
		if ms < 8400 || ms >= 20400 {
			return false
		}
		return (ms-8400)%1500 < 1200 // 1.2s on, 0.3s off, for 12s
	}
	end := runEP(t, sc, NewEndpointer(), 0, 8000, 30000)
	if end >= 0 && end < 20400 {
		t.Errorf("ended at %dms in the middle of a 12s sentence", end)
	}
	if end < 0 || end > 20400+1700 {
		t.Errorf("ended at %dms, the talker stopped at 20400ms", end)
	}
}

// Nothing said, nothing ended: the no-speech case belongs to the controller.
func TestNoEndWithoutSpeech(t *testing.T) {
	sc := newScene(900, 0, 1<<30, 1<<30)
	if end := runEP(t, sc, NewEndpointer(), 0, 8000, 30000); end >= 0 {
		t.Errorf("ended at %dms with nobody speaking", end)
	}
}

// Speech that began inside the blocks between the wake word and the session
// opening is counted.
func TestSpeechInTheBacklogCounts(t *testing.T) {
	// The wake word's last frame at 8000ms, the talker starting at 8450ms, the
	// session opening at 9000ms (550ms later), the talker stopping at 9500ms.
	sc := newScene(900, 4000, 8450, 9500)
	ep := NewEndpointer()
	end := -1
	for ms := 0; ms < 16000; ms += 80 {
		ep.Push(sc.next(1280))
		if ms+80 == 9040 {
			ep.StartSession(1040)
		}
		if end < 0 && ep.Ended() {
			end = ms + 80
		}
	}
	if end < 0 || end > 9500+1700 {
		t.Errorf("ended at %dms; talker stopped at 9500ms", end)
	}
}

func TestEndSessionReleasesTheFloor(t *testing.T) {
	ep := NewEndpointer()
	ep.StartSession(0)
	if !ep.est.freeze {
		t.Fatal("the floor is not held in a session")
	}
	ep.EndSession()
	if ep.est.freeze || ep.Ended() || ep.Heard() {
		t.Error("session state survived EndSession")
	}
}

// Eight seconds of speech with no pause longer than a syllable is more than the
// floor's four-second memory: without the floor held for the session, the
// command would become the floor and read as silence in the middle of itself.
func TestAnUnbrokenSentenceLongerThanTheFloorsMemoryDoesNotEnd(t *testing.T) {
	sc := newScene(900, 4000, 8400, 16400) // 8s, continuous
	end := runEP(t, sc, NewEndpointer(), 0, 8000, 24000)
	if end >= 0 && end < 16400 {
		t.Errorf("ended at %dms, inside an 8s unbroken sentence", end)
	}
	if end < 0 || end > 16400+1700 {
		t.Errorf("ended at %dms, the talker stopped at 16400ms", end)
	}
}

// The wake word's own last syllable is not the start of the command: said and
// followed by nothing, the session is not one that "was heard and then stopped".
func TestTheWakeWordsTailIsNotACommand(t *testing.T) {
	// The wake frame at 8000ms; the tail of the word runs to 8230ms; then silence.
	sc := newScene(900, 4000, 7600, 8230)
	ep := NewEndpointer()
	for ms := 0; ms < 20000; ms += 80 {
		ep.Push(sc.next(1280))
		if ms+80 == 8000 {
			ep.StartSession(0)
		}
		if ep.Heard() || ep.Ended() {
			t.Fatalf("at %dms: the wake word's tail was taken for a command", ms+80)
		}
	}
}
