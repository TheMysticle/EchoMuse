package schedule

import (
	"path/filepath"
	"testing"
	"time"
)

func alarmAt(id string, due time.Time) *Item {
	return &Item{ID: id, Kind: "alarm", Label: id, Due: due.Unix(), Zone: "UTC"}
}

func wasSignalled(e *Engine) bool {
	select {
	case <-e.Changed():
		return true
	default:
		return false
	}
}

// A timer sleeps for exactly its remaining time and rings the moment it is up,
// not a poll later.
func TestSleepIsExactForATimer(t *testing.T) {
	e, now, _ := fixture(t)
	apply(t, e, "create", "", timer("t", 45), 0)
	if d := e.Sleep(); d != 45*time.Second {
		t.Fatalf("sleeps %v, want 45s", d)
	}
	*now = now.Add(45*time.Second - time.Nanosecond)
	if e.Advance(); e.Snapshot().Active != "" {
		t.Fatal("rang a nanosecond early")
	}
	*now = now.Add(time.Nanosecond)
	if !e.Advance() || e.Snapshot().Active != "t" {
		t.Fatalf("not ringing at the deadline: %+v", e.Snapshot().Items)
	}
	if d := e.Sleep(); d != ringPoll {
		t.Fatalf("a ringing alert is watched every %v, got %v", ringPoll, d)
	}
}

// An alarm wakes for its wall-clock second, and exactly at it.
func TestSleepIsExactForAnAlarmDueNow(t *testing.T) {
	e, now, _ := fixture(t)
	due := now.Add(30 * time.Second)
	apply(t, e, "create", "", alarmAt("a", due), 0)
	if d := e.Sleep(); d != 30*time.Second && d != wallNear {
		t.Fatalf("sleeps %v, want 30s or the near re-plan %v", d, wallNear)
	}
	*now = due.Add(-time.Second)
	if d := e.Sleep(); d != time.Second {
		t.Fatalf("a second out sleeps %v, want 1s", d)
	}
	e.Advance()
	if e.Snapshot().Active != "" {
		t.Fatal("rang a second early")
	}
	*now = due
	if d := e.Sleep(); d != 0 {
		t.Fatalf("due now sleeps %v, want 0", d)
	}
	e.Advance()
	if e.Snapshot().Active != "a" {
		t.Fatal("did not ring at its second")
	}
}

// A device that was off at the due time: within the grace it still rings, past
// it the alarm is missed; either way the sleep before Advance is zero.
func TestDueInThePastIsHandledImmediately(t *testing.T) {
	for _, c := range []struct {
		late time.Duration
		want string
	}{{10 * time.Second, "ringing"}, {2 * time.Hour, "missed"}} {
		e, now, _ := fixture(t)
		due := now.Add(time.Minute)
		apply(t, e, "create", "", alarmAt("a", due), 0)
		restored, err := Open(e.path, "dot-A", e.now, e.valid)
		if err != nil {
			t.Fatal(err)
		}
		*now = due.Add(c.late)
		if d := restored.Sleep(); d != 0 {
			t.Fatalf("%v late: sleeps %v, want 0", c.late, d)
		}
		restored.Advance()
		got := restored.Snapshot().Items[0].State
		if c.want == "ringing" && got != "ringing" || c.want == "missed" && got != "missed" && got != "completed" {
			t.Fatalf("%v late: state %q, want %s", c.late, got, c.want)
		}
	}
}

// A command that changes what is due signals the loop and re-plans the sleep.
func TestACommandWhileSleepingReplansTheSleep(t *testing.T) {
	e, _, _ := fixture(t)
	apply(t, e, "create", "", timer("long", 3600), 0)
	if !wasSignalled(e) {
		t.Fatal("create did not signal the loop")
	}
	if d := e.Sleep(); d != time.Hour && d != maxIdle {
		t.Fatalf("sleeps %v", d)
	}
	apply(t, e, "create", "", timer("soon", 20), 0)
	if !wasSignalled(e) {
		t.Fatal("a sooner timer did not signal the loop")
	}
	if d := e.Sleep(); d != 20*time.Second {
		t.Fatalf("after a sooner timer sleeps %v, want 20s", d)
	}
	apply(t, e, "cancel", "soon", nil, 0)
	if !wasSignalled(e) {
		t.Fatal("cancel did not signal the loop")
	}
	if d := e.Sleep(); d == 20*time.Second {
		t.Fatal("a cancelled timer still sets the sleep")
	}
	// A failed command changes nothing, so it must not wake anybody.
	if _, err := e.Apply(Command{Owner: "dot-A", Action: "cancel", ID: "missing", Request: "nope"}); err == nil {
		t.Fatal("cancelling a missing item succeeded")
	}
	if wasSignalled(e) {
		t.Fatal("a failed command signalled the loop")
	}
}

// Stepping the wall clock must not make an alarm late or early: the plan for
// an alarm is recomputed from the clock every time, and is never more than the
// re-plan interval. (The fixture has one clock, so this does not show a timer
// ignoring a wall step; that rests on its deadline being a monotonic time.)
func TestAClockStepReplansAnAlarm(t *testing.T) {
	e, now, _ := fixture(t)
	start := *now
	due := start.Add(time.Hour)
	apply(t, e, "create", "", alarmAt("a", due), 0)
	apply(t, e, "create", "", timer("t", 3600), 0)
	if d := e.Sleep(); d > wallFar {
		t.Fatalf("an alarm an hour away sleeps %v, more than the %v re-plan", d, wallFar)
	}
	// The clock is stepped to five seconds before the alarm.
	*now = due.Add(-5 * time.Second)
	if d := e.Sleep(); d != 5*time.Second && d != wallNear {
		t.Fatalf("after the step the alarm sleeps %v, want <= 5s", d)
	}
	*now = due
	e.Advance()
	if s := e.Snapshot(); s.Active != "a" {
		t.Fatalf("alarm not ringing at its second after a step: %+v", s.Items)
	}
}

// Before the clock is trusted nothing can be due; the loop waits for the clock
// signal and is never asked to spin.
func TestSleepWhileTheClockIsNotTrusted(t *testing.T) {
	e, now, valid := fixture(t)
	apply(t, e, "create", "", alarmAt("a", now.Add(time.Hour)), 0)
	*valid = false
	if d := e.Sleep(); d != 0 {
		t.Fatalf("first look sleeps %v, want 0 so it can mark waiting_for_time", d)
	}
	e.Advance()
	if e.Snapshot().Items[0].State != "waiting_for_time" {
		t.Fatal("not waiting for time")
	}
	if d := e.Sleep(); d == 0 || d > wallFar {
		t.Fatalf("untrusted clock sleeps %v, want within (0, %v]", d, wallFar)
	}
	*valid = true
	e.Advance()
	if e.Snapshot().Items[0].State != "scheduled" {
		t.Fatalf("not rescheduled when the clock became trusted: %+v", e.Snapshot().Items[0])
	}
}

// Drive the engine only by what Sleep says, over a simulated hour, and check
// every alert rings at exactly its time and the loop does not busy-wake.
func TestDrivenBySleepAlertsRingExactlyOnTime(t *testing.T) {
	now := time.Date(2026, 10, 15, 8, 0, 0, 0, time.UTC)
	e, err := Open(filepath.Join(t.TempDir(), "state.json"), "dot-A", func() time.Time { return now }, func() bool { return true })
	if err != nil {
		t.Fatal(err)
	}
	start := now
	want := map[string]time.Time{
		"t1": start.Add(7 * time.Second),
		"t2": start.Add(19*time.Minute + 3*time.Second),
		"a1": start.Add(31 * time.Minute),
		"a2": start.Add(31*time.Minute + 30*time.Second),
	}
	mustApply := func(id, kind string, c Command) {
		c.Owner, c.Request, c.Action = "dot-A", id, "create"
		if _, err := e.Apply(c); err != nil {
			t.Fatal(err)
		}
	}
	mustApply("t1", "timer", Command{Item: timer("t1", 7)})
	mustApply("t2", "timer", Command{Item: timer("t2", 19*60+3)})
	mustApply("a1", "alarm", Command{Item: alarmAt("a1", want["a1"])})
	mustApply("a2", "alarm", Command{Item: alarmAt("a2", want["a2"])})
	rang := map[string]time.Time{}
	wakes := 0
	for now.Before(start.Add(time.Hour)) {
		wakes++
		e.Advance()
		for _, i := range e.Snapshot().Items {
			if _, seen := rang[i.ID]; !seen && (i.State == "ringing" || i.State == "queued") {
				rang[i.ID] = now
			}
		}
		d := e.Sleep()
		if a, _, ok := e.Active(); ok {
			// Dismiss like the user would, a little after it starts.
			now = now.Add(2 * time.Second)
			if _, err := e.Apply(Command{Owner: "dot-A", Request: "d" + a.ID, Action: "dismiss", ID: a.ID}); err != nil {
				t.Fatal(err)
			}
			continue
		}
		now = now.Add(max(d, 5*time.Millisecond))
	}
	for id, at := range want {
		got, ok := rang[id]
		if !ok {
			t.Errorf("%s never rang", id)
			continue
		}
		if got.Before(at) || got.Sub(at) > 2*time.Second {
			t.Errorf("%s rang at +%v, due +%v", id, got.Sub(start), at.Sub(start))
		}
	}
	// 100ms polling over this hour is 36000 wakes.
	if wakes > 1000 {
		t.Errorf("%d wakes in an hour", wakes)
	}
	t.Logf("%d wakes in a simulated hour with 4 alerts", wakes)
}
