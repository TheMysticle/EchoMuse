package beamformer

import (
	"encoding/binary"
	"math"
	"math/rand"
	"testing"
)

// A scene, sample by sample. The TV is noise whose level swings like dialogue:
// a slow random walk of +-4dB (about 1Hz) with the occasional loud line, which
// is what the three real recordings of 2026-10-12 looked like (5-14dB over the
// TV's own quiet moments, all the time). The talker is voiced speech under a
// 4Hz syllable envelope, set to sit about 5dB over the TV's 97th percentile as
// the real recordings did.
type epScene struct {
	rng    *rand.Rand
	t      int // samples
	tv     float64
	room   float64
	talker float64
	// speaking(t) says whether the talker is on at sample t.
	speaking func(t int) bool
	walk     float64
	lp       float64
}

func (s *epScene) next(n int) []byte {
	out := make([]byte, n*2)
	for i := 0; i < n; i++ {
		if s.t%128 == 0 { // every 8ms: the level wobble
			s.walk = 0.985*s.walk + 0.17*s.rng.NormFloat64()
		}
		lvl := math.Pow(10, math.Max(-6, math.Min(8, 4*s.walk))/20)
		w := s.rng.NormFloat64()
		s.lp = 0.9*s.lp + 0.1*w
		v := s.tv*lvl*(0.8*s.lp*3+0.6*w) + s.rng.NormFloat64()*s.room
		if s.speaking != nil && s.speaking(s.t) {
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

// newScene: tv is the TV's typical rms, start and stop (ms) bound the talker.
func newScene(tv, talker float64, start, stop int) *epScene {
	return &epScene{rng: rand.New(rand.NewSource(3)), tv: tv, room: 8, talker: talker,
		speaking: func(t int) bool { return t >= start*16 && t < stop*16 }}
}

const tvTalker = 9000 // over the TV's loud moments, as in the recordings

// runEP feeds the scene in 80ms chunks, opens the session at sessionAtMs, and
// returns when it ended (-1 if it did not).
func runEP(sc *epScene, ep *Endpointer, sessionAtMs, totalMs int) (endedAt int) {
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

// With dialogue on the TV the whole time and a command spoken over it, the
// command is over about a second after the talker stops, not when the TV does.
func TestEndsAboutASecondAfterTheTalkerStopsOverATV(t *testing.T) {
	sc := newScene(900, tvTalker, 10400, 13000)
	end := runEP(sc, NewEndpointer(), 10000, 24000)
	if end < 0 {
		t.Fatal("never ended with the TV on")
	}
	if end < 13000+800 || end > 13000+1700 {
		t.Errorf("ended at %dms; the talker stopped at 13000ms, expected 0.8-1.7s after", end)
	}
}

func TestEndsInAQuietRoomToo(t *testing.T) {
	sc := newScene(0, 3000, 10400, 12000)
	end := runEP(sc, NewEndpointer(), 10000, 20000)
	if end < 12000+800 || end > 12000+1700 {
		t.Errorf("ended at %dms, talker stopped at 12000ms", end)
	}
}

// A TV alone, dialogue and all, is never a command.
func TestATVAloneIsNeverSpeech(t *testing.T) {
	sc := newScene(900, 0, 1<<30, 1<<30)
	ep := NewEndpointer()
	for ms := 0; ms < 60000; ms += 80 {
		ep.Push(sc.next(1280))
		if ms == 10000 {
			ep.StartSession(0)
		}
		if ep.Heard() {
			t.Fatalf("heard speech at %dms in a TV with nobody talking", ms)
		}
	}
}

func TestAPauseInTheSentenceDoesNotEndIt(t *testing.T) {
	sc := newScene(900, tvTalker, 0, 0)
	sc.speaking = func(t int) bool {
		ms := t / 16
		return (ms >= 10400 && ms < 12000) || (ms >= 12600 && ms < 15000) // a 0.6s pause
	}
	end := runEP(sc, NewEndpointer(), 10000, 24000)
	if end >= 0 && end < 15000 {
		t.Errorf("ended at %dms, inside the sentence (pause 12.0-12.6s)", end)
	}
	if end < 0 {
		t.Error("never ended after the sentence")
	}
}

// Eight seconds of speech with no pause longer than a syllable. The reference
// is fixed when the command starts, so the command cannot become it.
func TestAnUnbrokenSentenceDoesNotEndInTheMiddle(t *testing.T) {
	sc := newScene(900, tvTalker, 10400, 18400)
	end := runEP(sc, NewEndpointer(), 10000, 28000)
	if end >= 0 && end < 18400 {
		t.Errorf("ended at %dms, inside an 8s unbroken sentence", end)
	}
	if end < 0 || end > 18400+1700 {
		t.Errorf("ended at %dms, the talker stopped at 18400ms", end)
	}
}

func TestNoEndWithoutSpeech(t *testing.T) {
	sc := newScene(900, 0, 1<<30, 1<<30)
	if end := runEP(sc, NewEndpointer(), 10000, 30000); end >= 0 {
		t.Errorf("ended at %dms with nobody speaking", end)
	}
}

// The wake word's own last syllable is not the command.
func TestTheWakeWordsTailIsNotACommand(t *testing.T) {
	sc := newScene(900, tvTalker, 9600, 10230) // the word ends 230ms after its frame
	ep := NewEndpointer()
	for ms := 0; ms < 24000; ms += 80 {
		ep.Push(sc.next(1280))
		if ms+80 == 10000 {
			ep.StartSession(0)
		}
		if ep.Heard() || ep.Ended() {
			t.Fatalf("at %dms: the wake word's tail was taken for a command", ms+80)
		}
	}
}

// Speech that began inside the blocks between the wake word and the session
// opening is counted.
func TestSpeechInTheBacklogCounts(t *testing.T) {
	// The wake word's last frame at 10000ms, the talker from 10450ms, the session
	// opening at 11040ms (1040ms later), the talker stopping at 11500ms.
	sc := newScene(900, tvTalker, 10450, 11500)
	ep := NewEndpointer()
	end := -1
	for ms := 0; ms < 20000; ms += 80 {
		ep.Push(sc.next(1280))
		if ms+80 == 11040 {
			ep.StartSession(1040)
		}
		if end < 0 && ep.Ended() {
			end = ms + 80
		}
	}
	if end < 0 || end > 11500+1700 {
		t.Errorf("ended at %dms; talker stopped at 11500ms", end)
	}
}

// With under six seconds of the room to go on there is no reference, and then
// nothing is ever ended: the controller keeps its own ending.
func TestWithoutHistoryItLeavesTheControllerAlone(t *testing.T) {
	sc := newScene(900, tvTalker, 3400, 4500)
	ep := NewEndpointer()
	for ms := 0; ms < 12000; ms += 80 {
		ep.Push(sc.next(1280))
		if ms+80 == 3040 {
			ep.StartSession(0)
		}
		if ep.Ended() {
			t.Fatalf("ended at %dms with 3s of history", ms+80)
		}
	}
	if ep.Warm() {
		t.Error("warm with 3s of history")
	}
}

func TestEndSessionClearsTheSession(t *testing.T) {
	ep := NewEndpointer()
	ep.StartSession(0)
	ep.EndSession()
	if ep.Ended() || ep.Heard() || ep.Warm() {
		t.Error("session state survived EndSession")
	}
}
