package schedule

import (
	"encoding/json"
	"fmt"
	"testing"
	"time"
)

// The controller shares an alert by keeping a copy in another satellite's own
// store, made with these ordinary commands (controller/em_schedule_share.py).
// The commands below are the JSON it sends, so a change here that the
// controller's copies depend on fails in this package, not on an Echo.
const mirrorPrefix = "sh-0a1b2c3d-"

func mirror(n int) string { return fmt.Sprintf("%s%024x", mirrorPrefix, n) }

func send(t *testing.T, e *Engine, raw string) (Snapshot, error) {
	t.Helper()
	var c Command
	if err := json.Unmarshal([]byte(raw), &c); err != nil {
		t.Fatal(err)
	}
	c.Owner = "dot-A"
	c.Request = fmt.Sprintf("sh_%d", time.Now().UnixNano())
	return e.Apply(c)
}

func item(s Snapshot, id string) *Item {
	for i := range s.Items {
		if s.Items[i].ID == id {
			return &s.Items[i]
		}
	}
	return nil
}

func TestACopyIsMadeFromTheCommandsTheControllerSends(t *testing.T) {
	e, now, _ := fixture(t)
	due := now.Add(time.Hour).Unix()
	id := mirror(1)
	if len(id) > 80 || !idPattern.MatchString(id) {
		t.Fatalf("a copy's id is not a legal item id: %q", id)
	}
	s, err := send(t, e, fmt.Sprintf(`{"action":"create","item":{"kind":"reminder","label":"Bins","timezone":"UTC","message":"Bins","due":%d,"id":%q}}`, due, id))
	if err != nil {
		t.Fatal(err)
	}
	if got := item(s, id); got == nil || got.State != "scheduled" || got.Due != due || got.Message != "Bins" {
		t.Fatalf("copy not stored as sent: %+v", got)
	}
	// Sending the same create again (a retry after a lost reply) is refused, not doubled.
	if _, err := send(t, e, fmt.Sprintf(`{"action":"create","item":{"kind":"reminder","label":"Bins","timezone":"UTC","due":%d,"id":%q}}`, due, id)); err == nil {
		t.Fatal("duplicate copy accepted")
	}
	// Metadata-only edit keeps the copy's state; its own time is sent back unchanged.
	s, err = send(t, e, fmt.Sprintf(`{"action":"edit","id":%q,"item":{"kind":"reminder","label":"Bins out","timezone":"UTC","message":"Put the bins out","due":%d,"id":%q}}`, id, due, id))
	if err != nil {
		t.Fatal(err)
	}
	if got := item(s, id); got.Message != "Put the bins out" || got.State != "scheduled" {
		t.Fatalf("edit: %+v", got)
	}
}

func TestACopyOfARecurringAlarmGetsItsNextTimeFromItsOwnClock(t *testing.T) {
	e, _, _ := fixture(t)
	id := mirror(2)
	s, err := send(t, e, fmt.Sprintf(`{"action":"create","item":{"kind":"alarm","label":"Wake","timezone":"UTC","due":0,"rule":{"days":[1,2,3],"interval":0,"hour":7,"minute":30},"id":%q}}`, id))
	if err != nil {
		t.Fatal(err)
	}
	if got := item(s, id); got.Due == 0 || got.Rule == nil || got.Rule.Hour != 7 {
		t.Fatalf("recurring copy: %+v", got)
	}
}

func TestATimerCopyPausesAndResumesWithTheOriginal(t *testing.T) {
	e, _, _ := fixture(t)
	id := mirror(3)
	s, err := send(t, e, fmt.Sprintf(`{"action":"create","item":{"kind":"timer","label":"Pasta","timezone":"UTC","duration":600,"id":%q}}`, id))
	if err != nil {
		t.Fatal(err)
	}
	copy := item(s, id)
	due, duration := copy.Due, copy.Duration
	if s, err = send(t, e, fmt.Sprintf(`{"action":"pause","id":%q}`, id)); err != nil || item(s, id).State != "paused" {
		t.Fatalf("pause: %v %+v", err, item(s, id))
	}
	// A label change while paused keeps it paused: the copy's own numbers go back unchanged.
	s, err = send(t, e, fmt.Sprintf(`{"action":"edit","id":%q,"item":{"kind":"timer","label":"Rice","timezone":"UTC","due":%d,"duration":%d,"id":%q}}`, id, due, duration, id))
	if err != nil || item(s, id).State != "paused" || item(s, id).Label != "Rice" {
		t.Fatalf("metadata edit of a paused copy: %v %+v", err, item(s, id))
	}
	// A different duration restarts it from the original's remaining time.
	s, err = send(t, e, fmt.Sprintf(`{"action":"edit","id":%q,"item":{"kind":"timer","label":"Rice","timezone":"UTC","duration":420,"id":%q}}`, id, id))
	if err != nil || item(s, id).State != "scheduled" || item(s, id).Duration != 420 {
		t.Fatalf("re-timed copy: %v %+v", err, item(s, id))
	}
	if s, err = send(t, e, fmt.Sprintf(`{"action":"pause","id":%q}`, id)); err != nil {
		t.Fatal(err)
	}
	if s, err = send(t, e, fmt.Sprintf(`{"action":"resume","id":%q}`, id)); err != nil || item(s, id).State != "scheduled" {
		t.Fatalf("resume: %v", err)
	}
}

func TestStoppingTogetherUsesDismissForWhatRingsAndCompleteForWhatIsAboutTo(t *testing.T) {
	e, now, _ := fixture(t)
	ringing, soon := mirror(4), mirror(6)
	for _, c := range []struct {
		id      string
		seconds int
	}{{ringing, 10}, {soon, 14}} {
		if _, err := send(t, e, fmt.Sprintf(`{"action":"create","item":{"kind":"timer","label":"t","timezone":"UTC","duration":%d,"id":%q}}`, c.seconds, c.id)); err != nil {
			t.Fatal(err)
		}
	}
	// Dismiss only applies to what rings, so the controller does not send it to anything else.
	if _, err := send(t, e, fmt.Sprintf(`{"action":"dismiss","id":%q}`, soon)); err == nil {
		t.Fatal("dismiss of a scheduled item accepted")
	}
	*now = now.Add(11 * time.Second)
	e.Tick()
	s, _ := send(t, e, `{"action":"list"}`)
	if item(s, ringing).State != "ringing" {
		t.Fatalf("not ringing: %+v", item(s, ringing))
	}
	s, err := send(t, e, fmt.Sprintf(`{"action":"dismiss","id":%q}`, ringing))
	if err != nil || item(s, ringing).State != "completed" {
		t.Fatalf("dismiss: %v %+v", err, item(s, ringing))
	}
	// An item three seconds from ringing is finished without ever sounding.
	s, err = send(t, e, fmt.Sprintf(`{"action":"complete","id":%q}`, soon))
	if err != nil || item(s, soon).State != "completed" {
		t.Fatalf("complete: %v %+v", err, item(s, soon))
	}
	e.Tick()
	if s, _ = send(t, e, `{"action":"list"}`); s.Active != "" {
		t.Fatalf("something is still ringing: %q", s.Active)
	}
}

func TestACopyIsCancelledByIdAndAnUnknownOneIsAnError(t *testing.T) {
	e, now, _ := fixture(t)
	id := mirror(8)
	if _, err := send(t, e, fmt.Sprintf(`{"action":"create","item":{"kind":"reminder","label":"x","timezone":"UTC","due":%d,"id":%q}}`, now.Add(time.Hour).Unix(), id)); err != nil {
		t.Fatal(err)
	}
	if _, err := send(t, e, fmt.Sprintf(`{"action":"cancel","id":%q}`, id)); err != nil {
		t.Fatal(err)
	}
	if _, err := send(t, e, fmt.Sprintf(`{"action":"cancel","id":%q}`, mirror(9))); err == nil {
		t.Fatal("cancel of an unknown copy accepted")
	}
}
