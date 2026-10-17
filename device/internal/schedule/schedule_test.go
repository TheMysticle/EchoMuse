package schedule

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func fixture(t *testing.T) (*Engine, *time.Time, *bool) {
	t.Helper()
	now := time.Date(2026, 10, 15, 8, 0, 0, 0, time.UTC)
	valid := true
	e, err := Open(filepath.Join(t.TempDir(), "state.json"), "dot-A", func() time.Time { return now }, func() bool { return valid })
	if err != nil {
		t.Fatal(err)
	}
	return e, &now, &valid
}
func apply(t *testing.T, e *Engine, action, id string, item *Item, seconds int64) Snapshot {
	t.Helper()
	s, err := e.Apply(Command{Owner: "dot-A", Request: action + id + strings.ReplaceAll(time.Now().Format("150405.000000000"), ".", "_"), Action: action, ID: id, Item: item, Seconds: seconds})
	if err != nil {
		t.Fatal(err)
	}
	return s
}
func timer(id string, seconds int64) *Item {
	return &Item{ID: id, Kind: "timer", Label: id, Duration: seconds, Zone: "UTC"}
}
func TestDurableLongTimerAndOwnership(t *testing.T) {
	e, now, _ := fixture(t)
	apply(t, e, "create", "", timer("long", 86400*90), 0)
	path := e.path
	restored, err := Open(path, "dot-A", e.now, e.valid)
	if err != nil {
		t.Fatal(err)
	}
	*now = now.Add(90 * 24 * time.Hour)
	s, _ := restored.Tick()
	if s.Active != "long" {
		t.Fatalf("not ringing: %+v", s)
	}
	if _, err := Open(path, "dot-B", e.now, e.valid); err == nil {
		t.Fatal("cross-device store accepted")
	}
	if _, err := e.Apply(Command{Owner: "dot-B", Action: "cancel", ID: "long", Request: "x"}); err == nil {
		t.Fatal("cross-device mutation accepted")
	}
}
func TestIdempotenceAndFailedPersistence(t *testing.T) {
	e, _, _ := fixture(t)
	c := Command{Owner: "dot-A", Action: "create", Request: "same", Item: timer("one", 60)}
	a, err := e.Apply(c)
	if err != nil {
		t.Fatal(err)
	}
	b, err := e.Apply(c)
	if err != nil || a.Revision != b.Revision || len(b.Items) != 1 {
		t.Fatal("retry duplicated mutation")
	}
	e.persist = func(State, State) error { return errors.New("disk full") }
	if _, err := e.Apply(Command{Owner: "dot-A", Action: "cancel", ID: "one", Request: "cancel"}); err == nil {
		t.Fatal("failed disk write acknowledged")
	}
	if e.Snapshot().Items[0].State != "scheduled" {
		t.Fatal("failed write changed state")
	}
}
func TestQueueSnoozeAndCleanup(t *testing.T) {
	e, now, _ := fixture(t)
	apply(t, e, "create", "", timer("timer", 5), 0)
	a := timer("alarm", 5)
	a.Kind = "alarm"
	apply(t, e, "create", "", a, 0)
	r := timer("reminder", 5)
	r.Kind = "reminder"
	apply(t, e, "create", "", r, 0)
	*now = now.Add(5 * time.Second)
	s, _ := e.Tick()
	if s.Active != "alarm" {
		t.Fatal(s.Active)
	}
	s = apply(t, e, "snooze", "alarm", nil, 60)
	if s.Active != "timer" {
		t.Fatal(s.Active)
	}
	s = apply(t, e, "dismiss", "timer", nil, 0)
	if s.Active != "reminder" {
		t.Fatal(s.Active)
	}
	*now = now.Add(60 * time.Second)
	s, _ = e.Tick()
	if s.Active != "alarm" {
		t.Fatal(s.Active)
	}
	*now = now.Add(2 * time.Hour)
	s, _ = e.Tick()
	if s.Active != "reminder" {
		t.Fatalf("queued reminder lost its ring budget: %+v", s.Items)
	}
	apply(t, e, "dismiss", "reminder", nil, 0)
	s, _ = e.Tick()
	if len(s.Items) != 0 {
		t.Fatalf("terminal items retained: %+v", s.Items)
	}
}
func TestPauseResumeAndAdjustment(t *testing.T) {
	e, now, _ := fixture(t)
	apply(t, e, "create", "", timer("x", 120), 0)
	*now = now.Add(30 * time.Second)
	s := apply(t, e, "pause", "x", nil, 0)
	if s.Items[0].Remaining != 90 {
		t.Fatal(s)
	}
	*now = now.Add(24 * time.Hour)
	e.Tick()
	apply(t, e, "adjust", "x", nil, 30)
	s = apply(t, e, "resume", "x", nil, 0)
	if s.Items[0].Due != now.Unix()+120 {
		t.Fatal(s)
	}
	*now = now.Add(120 * time.Second)
	s, _ = e.Tick()
	if s.Active != "x" {
		t.Fatal(s)
	}
}
func TestUnknownTimeOnReboot(t *testing.T) {
	e, _, valid := fixture(t)
	apply(t, e, "create", "", timer("x", 60), 0)
	*valid = false
	restored, err := Open(e.path, "dot-A", e.now, e.valid)
	if err != nil {
		t.Fatal(err)
	}
	s, _ := restored.Tick()
	if s.Active != "" || s.Items[0].State != "waiting_for_time" {
		t.Fatal(s)
	}
	*valid = true
	s, _ = restored.Tick()
	if s.Items[0].State != "scheduled" {
		t.Fatal(s)
	}
}
func TestCalendarDSTAndRecurrence(t *testing.T) {
	i := Item{Zone: "Europe/Warsaw", Rule: &Rule{Days: []int{0}, Hour: 2, Minute: 30}}
	loc, _ := time.LoadLocation(i.Zone)
	spring := time.Date(2026, 3, 28, 23, 0, 0, 0, loc)
	next := nextOccurrence(i, spring).In(loc)
	if next.Day() != 29 || next.Hour() != 3 || next.Minute() != 0 {
		t.Fatal(next)
	}
	fall := time.Date(2026, 10, 24, 23, 0, 0, 0, loc)
	next = nextOccurrence(i, fall).In(loc)
	_, offset := next.Zone()
	if next.Day() != 25 || next.Hour() != 2 || offset != 7200 {
		t.Fatal(next)
	}
	again := nextOccurrence(i, next).In(loc)
	if again.Day() != 1 || again.Month() != 11 {
		t.Fatal("repeated DST occurrence", again)
	}
}
func TestCorruptSnapshotRecoversPrevious(t *testing.T) {
	e, _, _ := fixture(t)
	apply(t, e, "create", "", timer("one", 60), 0)
	apply(t, e, "create", "", timer("two", 60), 0)
	if err := os.WriteFile(e.path, []byte("truncated"), 0600); err != nil {
		t.Fatal(err)
	}
	other, err := Open(e.path, "dot-A", e.now, e.valid)
	if err != nil {
		t.Fatal(err)
	}
	if len(other.Snapshot().Items) != 1 {
		t.Fatal("did not recover previous")
	}
}
func TestPastDeadlineAndStaleRevision(t *testing.T) {
	e, now, _ := fixture(t)
	_, err := e.Apply(Command{Owner: "dot-A", Request: "old", Action: "create", Item: &Item{ID: "past", Kind: "alarm", Due: now.Unix() - 1, Zone: "UTC"}})
	if err == nil {
		t.Fatal("past accepted")
	}
	apply(t, e, "create", "", timer("one", 60), 0)
	rev := uint64(0)
	if _, err = e.Apply(Command{Owner: "dot-A", Request: "stale", Action: "cancel", ID: "one", Revision: &rev}); err == nil {
		t.Fatal("stale accepted")
	}
}
func TestLimits(t *testing.T) {
	e, _, _ := fixture(t)
	for n := 0; n < MaxItems; n++ {
		apply(t, e, "create", "", timer(time.Unix(int64(n), 0).Format("150405"), 60), 0)
	}
	if _, err := e.Apply(Command{Owner: "dot-A", Request: "overflow", Action: "create", Item: timer("extra", 60)}); err == nil {
		t.Fatal("limit not enforced")
	}
}

func TestEditingSoundDoesNotRestartTimer(t *testing.T) {
	e, now, _ := fixture(t)
	s := apply(t, e, "create", "", timer("one", 120), 0)
	*now = now.Add(30 * time.Second)
	i := s.Items[0]
	i.Label = "tea"
	s = apply(t, e, "edit", "one", &i, 0)
	if s.Items[0].Due != i.Due {
		t.Fatal("metadata edit moved deadline")
	}
	*now = now.Add(90 * time.Second)
	s, _ = e.Tick()
	if s.Active != "one" {
		t.Fatal("metadata edit restarted timer")
	}
}
func TestRingCapWorksBeforeTimeSync(t *testing.T) {
	e, now, valid := fixture(t)
	*valid = false
	apply(t, e, "create", "", timer("one", 2), 0)
	*now = now.Add(2 * time.Second)
	s, _ := e.Tick()
	if s.Active != "one" {
		t.Fatal(s)
	}
	*now = now.Add(601 * time.Second)
	s, _ = e.Tick()
	if s.Active != "" {
		t.Fatal("unsynchronized ring ignored cap")
	}
}
func TestFailedOccurrenceCommitRetainsDeadline(t *testing.T) {
	e, now, _ := fixture(t)
	apply(t, e, "create", "", timer("one", 2), 0)
	*now = now.Add(2 * time.Second)
	persist := e.persist
	e.persist = func(State, State) error { return errors.New("disk full") }
	s, _ := e.Tick()
	if s.Active != "" {
		t.Fatal("uncommitted occurrence played")
	}
	if _, ok := e.deadlines["one"]; !ok {
		t.Fatal("failed transaction lost monotonic deadline")
	}
	e.persist = persist
	s, _ = e.Tick()
	if s.Active != "one" {
		t.Fatal(s)
	}
}
func TestColdBootDoesNotReplayRingWithoutTime(t *testing.T) {
	e, now, valid := fixture(t)
	apply(t, e, "create", "", timer("one", 1), 0)
	*now = now.Add(time.Second)
	e.Tick()
	*valid = false
	other, err := Open(e.path, "dot-A", e.now, e.valid)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, active := other.Active(); active {
		t.Fatal("unverified restored occurrence played")
	}
	*valid = true
	*now = now.Add(2 * time.Hour)
	other.Tick()
	if _, _, active := other.Active(); active {
		t.Fatal("expired restored occurrence replayed")
	}
}

func TestCalendarHandlesAZoneThatSkipsMidnight(t *testing.T) {
	loc, err := time.LoadLocation("America/Sao_Paulo")
	if err != nil {
		t.Fatal(err)
	}
	i := Item{Zone: "America/Sao_Paulo", Rule: &Rule{Days: []int{0}, Hour: 0, Minute: 0}}
	next := nextOccurrence(i, time.Date(2018, 11, 3, 12, 0, 0, 0, loc)).In(loc)
	if next.Day() != 4 || next.Hour() != 1 {
		t.Fatal(next)
	}
}
