package speaker

import (
	"testing"
	"time"
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
