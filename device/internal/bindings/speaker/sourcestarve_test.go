package speaker

import (
	"testing"
	"time"
)

func TestAStarveIsOnlyCountedOnceTheSourceHasPlayed(t *testing.T) {
	// Sendspin places audio in time and declines every period until its
	// scheduled start, so a source that has never filled one is starting up,
	// not starving. Counting those reports a fault at the top of every track.
	var s SourceStarve
	now := time.Unix(1000, 0)
	if total, log := s.Note(now, 0); total != 0 || log {
		t.Errorf("a source that never played counted a starve (total=%d log=%v)", total, log)
	}
	if s.Total() != 0 {
		t.Errorf("total is %d, want 0", s.Total())
	}
}

func TestAStoppedSourceIsNotStarving(t *testing.T) {
	// Active() can linger after the audio has genuinely ended. A decline
	// long after the last filled period is that, not a missed one.
	var s SourceStarve
	now := time.Unix(1000, 0)
	played := now.Add(-StarveWindow - time.Millisecond).UnixNano()
	if _, log := s.Note(now, played); log {
		t.Error("a stopped source logged a starve")
	}
	if s.Total() != 0 {
		t.Errorf("total is %d, want 0", s.Total())
	}
}

func TestAMissedPeriodIsCountedAndLogged(t *testing.T) {
	var s SourceStarve
	now := time.Unix(1000, 0)
	total, log := s.Note(now, now.Add(-20*time.Millisecond).UnixNano())
	if total != 1 || !log {
		t.Errorf("first starve: total=%d log=%v, want 1 true", total, log)
	}
}

func TestEveryStarveIsCountedButTheLogIsRateLimited(t *testing.T) {
	// The count is the diagnosis; the line is just how it surfaces. The
	// device log is a fixed-size ram_console shared with crash evidence, so
	// a source starving every period must not fill it.
	var s SourceStarve
	base := time.Unix(1000, 0)
	logs := 0
	for i := 0; i < 500; i++ {
		// One period is ~42.7ms; 500 of them is ~21s, so two windows.
		now := base.Add(time.Duration(i) * 42700 * time.Microsecond)
		if _, log := s.Note(now, now.Add(-time.Millisecond).UnixNano()); log {
			logs++
		}
	}
	if s.Total() != 500 {
		t.Errorf("counted %d starves, want all 500", s.Total())
	}
	if logs != 3 {
		t.Errorf("logged %d times over ~21s at %v, want 3", logs, StarveLogEvery)
	}
}

func TestTheCountSurvivesANonStarvingDecline(t *testing.T) {
	// A real stream starves, plays on, ends. The total must not be reset by
	// the innocent declines around it — it is a since-boot figure, and a
	// counter that quietly rezeroes reads as a healthy device.
	var s SourceStarve
	now := time.Unix(1000, 0)
	s.Note(now, now.Add(-time.Millisecond).UnixNano())
	s.Note(now.Add(time.Second), 0) // starting up again
	if s.Total() != 1 {
		t.Errorf("total is %d, want 1", s.Total())
	}
}
