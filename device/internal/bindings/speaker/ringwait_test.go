package speaker

import (
	"testing"
	"time"

	"github.com/wilbowes/EchoMuse/internal/outchain"
)

func TestRingWait(t *testing.T) {
	const target = shallowRingFrames
	if got := ringWait(target, target); got != 0 {
		t.Errorf("at target: %v", got)
	}
	if got := ringWait(target-1, target); got != 0 {
		t.Errorf("under target: %v", got)
	}
	// 48 frames over target is exactly 1ms.
	if got := ringWait(target+48, target); got != time.Millisecond {
		t.Errorf("48 frames over: %v", got)
	}
	// A full ring (4 x 2048) drains to target within one wait, which has to
	// outlast a period: with the cap at 40ms the ring never came down.
	full := ringFrames
	want := time.Duration(full-target) * time.Second / 48000
	if want > maxRingWait {
		t.Fatalf("a full ring needs %v, over the %v cap", want, maxRingWait)
	}
	if got := ringWait(full, target); got != want {
		t.Errorf("full ring: %v, want %v", got, want)
	}
	if maxRingWait <= time.Duration(loopPeriodFrames)*time.Second/48000 {
		t.Errorf("cap %v does not outlast a period", maxRingWait)
	}
	// Anything absurd is bounded.
	if got := ringWait(1<<30, target); got != maxRingWait {
		t.Errorf("huge delay: %v", got)
	}
}

// The shallow target is a trade measured on hardware; the period it implies
// must fit inside the ring or the write would block on every turn.
func TestShallowTargetFitsTheRing(t *testing.T) {
	if shallowRingFrames+loopPeriodFrames > ringFrames {
		t.Fatalf("target %d + period %d exceeds the ring %d",
			shallowRingFrames, loopPeriodFrames, ringFrames)
	}
	if shallowRingFrames < MarginFloorFrames {
		t.Fatalf("target %d is under the margin floor %d", shallowRingFrames, MarginFloorFrames)
	}
}

func TestSourcePeriodBytes(t *testing.T) {
	whole, short := loopPeriodFrames*4, lowLatencyPeriodFrames*4
	cases := []struct {
		voice, low bool
		want       int
	}{
		{false, false, whole}, // Spotify, Music Assistant's 2s lead: unchanged
		{false, true, short},  // a low-latency stream alone
		{true, true, whole},   // a voice period is whole and the music mixes against it
		{true, false, whole},
	}
	for _, c := range cases {
		if got := sourcePeriodBytes(c.voice, c.low); got != c.want {
			t.Errorf("voice=%v low=%v: %d bytes, want %d", c.voice, c.low, got, c.want)
		}
	}
}

// A duck is a rate, rampStep per period. Fed half-periods it must still take
// the same time, which is two mixes, and never arrive early or late.
func TestDuckTakesTheSameTimeOnHalfPeriods(t *testing.T) {
	target := DuckGain(-18)
	whole := &Mixer{gain: unityGain}
	whole.applyGain(period(loopPeriodFrames, 1000), target)

	half := &Mixer{gain: unityGain}
	half.SetSlices(2)
	half.applyGain(period(loopPeriodFrames/2, 1000), target)
	half.applyGain(period(loopPeriodFrames/2, 1000), target)
	if half.Gain() != whole.Gain() {
		t.Fatalf("two half-periods reached %d, one whole period reached %d", half.Gain(), whole.Gain())
	}

	// Back to whole periods the default is untouched, bit for bit.
	half.SetSlices(1)
	ref := &Mixer{gain: half.Gain()}
	half.applyGain(period(loopPeriodFrames, 1000), unityGain)
	ref.applyGain(period(loopPeriodFrames, 1000), unityGain)
	if half.Gain() != ref.Gain() {
		t.Fatalf("after SetSlices(1) the ramp differs: %d vs %d", half.Gain(), ref.Gain())
	}
}

func TestSlicesDefaultChangesNothing(t *testing.T) {
	a, b := &Mixer{gain: unityGain}, &Mixer{gain: unityGain, slices: 1}
	for i := 0; i < 5; i++ {
		a.applyGain(period(64, 1000), DuckGain(-30))
		b.applyGain(period(64, 1000), DuckGain(-30))
		if a.Gain() != b.Gain() {
			t.Fatalf("step %d: %d vs %d", i, a.Gain(), b.Gain())
		}
	}
}

// The chain's FIR is sized for the longest block the speaker hands it. If the
// loop period grew past that, the first whole period would panic on a Radar.
func TestLoopPeriodFitsTheChainFIR(t *testing.T) {
	if loopPeriodFrames > outchain.MaxPeriodFrames {
		t.Fatalf("the loop period is %d frames, the chain's FIR is sized for %d",
			loopPeriodFrames, outchain.MaxPeriodFrames)
	}
}

// The minimum queue-plus-build budget must leave headroom below 80ms.
func TestLowLatencyPullBudget(t *testing.T) {
	budget := time.Duration(shallowRingFrames+lowLatencyPeriodFrames) * time.Second / 48000
	if budget >= 75*time.Millisecond {
		t.Fatalf("queue + pull = %v; want less than 75ms", budget)
	}
}

func TestQuarterPeriodsKeepDuckRate(t *testing.T) {
	whole := &Mixer{gain: unityGain}
	quarter := &Mixer{gain: unityGain}
	quarter.SetSlices(4)
	for _, target := range []int32{0, unityGain, DuckGain(-18), unityGain} {
		whole.applyGain(period(loopPeriodFrames, 1000), target)
		for i := 0; i < 4; i++ {
			quarter.applyGain(period(loopPeriodFrames/4, 1000), target)
		}
		if whole.Gain() != quarter.Gain() {
			t.Fatalf("target %d: whole %d, quarter %d", target, whole.Gain(), quarter.Gain())
		}
	}
}

func TestVoiceSliceCoversThePeriodInLowLatencySlices(t *testing.T) {
	v := make([]byte, loopPeriodFrames*4+100)
	for i := range v {
		v[i] = byte(i)
	}
	var got []byte
	for rest := v; len(rest) > 0; {
		var s []byte
		s, rest = voiceSlice(rest)
		if len(s) > lowLatencyPeriodFrames*4 {
			t.Fatalf("slice of %d bytes, want at most %d", len(s), lowLatencyPeriodFrames*4)
		}
		got = append(got, s...)
	}
	if string(got) != string(v) {
		t.Fatal("slices do not reassemble the period")
	}
}
