package beamformer

import (
	"math"
	"math/rand"
	"testing"
)

// Stock's merger moves to a group only after it has WON 125 frames (1s), however
// decisively it leads. The votes accumulate and are not reset by another group
// winning a frame, so the delay after a talker starts can be a little under a
// second when the group had already won some by chance; the first rule moves in
// about a tenth.
func TestStockMergerNeedsASecondOfVotesToMove(t *testing.T) {
	b := New()
	enableWakeBeam(b)
	b.SetWakeBeamStock(true)
	rng := rand.New(rand.NewSource(8))
	src := denseTones(rng, 200, 3500, 100)
	const period = 2560
	burst := func(from int) func(int) float64 {
		return func(t int) float64 {
			if t >= from {
				return 1
			}
			return 0
		}
	}
	// Room noise to settle, then a steady talker from 55 degrees from 7s.
	var at, changedAt int = 0, -1
	talkFrom := 7 * 16000
	last := -2
	for k := 0; k < 20*16000/period; k++ {
		x := zeros(NumMics, period)
		render(x, src, 55, at, burst(talkFrom))
		for ch := range x {
			for i := range x[ch] {
				x[ch][i] += rng.NormFloat64() * 5
			}
		}
		b.WakeBeam(s16(x))
		at += period
		g := b.WakeBeamIndex()
		if at > talkFrom && changedAt < 0 && last >= 0 && g != last {
			changedAt = at
		}
		last = g
	}
	if changedAt < 0 {
		t.Fatal("the selection never moved to the talker")
	}
	if d := changedAt - talkFrom; d < 16000*4/10 {
		t.Errorf("moved %dms after the talker started; stock needs 125 votes", d*1000/16000)
	}
}

// Inside the group the beams are weighted by their share of the score, and the
// weights always sum to one so the level does not depend on the scores.
func TestStockMergerWeightsAreSharesThatSumToOne(t *testing.T) {
	b := New()
	enableWakeBeam(b)
	b.SetWakeBeamStock(true)
	rng := rand.New(rand.NewSource(9))
	src := denseTones(rng, 200, 3500, 100)
	at := 0
	for k := 0; k < 12*16000/2560; k++ {
		x := zeros(NumMics, 2560)
		render(x, src, 200, at, func(t int) float64 { return 1 })
		for ch := range x {
			for i := range x[ch] {
				x[ch][i] += rng.NormFloat64() * 5
			}
		}
		b.WakeBeam(s16(x))
		at += 2560
	}
	w := b.fbf
	if w.cur == fbfNeutral {
		t.Fatal("still on the centre mic")
	}
	var sum float64
	for _, v := range w.wts {
		if v < 0 {
			t.Errorf("negative weight %v", v)
		}
		sum += float64(v)
	}
	if math.Abs(sum-1) > 1e-4 {
		t.Errorf("weights %v sum to %v, want 1", w.wts, sum)
	}
	// A talker off to one side makes one beam of the group better than its
	// neighbours; shares differ, an equal mean would not.
	lo, hi := float32(1), float32(0)
	for _, v := range w.wts {
		lo, hi = min(lo, v), max(hi, v)
	}
	if hi-lo < 0.02 {
		t.Errorf("weights %v are equal shares; stock weights each beam by its share of the score", w.wts)
	}
}

// The mode is a per-path setting, and switching it back restores the first rule.
func TestWakeBeamStockSwitchesBothRules(t *testing.T) {
	b := New()
	enableWakeBeam(b)
	x := s16(zeros(NumMics, 2560))
	b.WakeBeam(x)
	if b.fbf.group != GroupMean || b.fbf.ara.gmode != ARAGateOurs {
		t.Fatal("not the first rule by default")
	}
	b.SetWakeBeamStock(true)
	b.WakeBeam(x)
	if b.fbf.group != GroupStock || b.fbf.ara.gmode != ARAGateStock {
		t.Error("wakeBeamStock did not select both of stock's rules")
	}
	b.SetWakeBeamStock(false)
	b.WakeBeam(x)
	if b.fbf.group != GroupMean || b.fbf.ara.gmode != ARAGateOurs {
		t.Error("turning it off did not restore the first rule")
	}
}
