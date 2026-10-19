package clock

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func lineAt(t *testing.T, zone string, at time.Time, msg string) string {
	t.Helper()
	var buf bytes.Buffer
	w := &zoneWriter{out: &buf, now: func() time.Time { return at }}
	if zone != "" {
		loc, err := time.LoadLocation(zone)
		if err != nil {
			t.Fatalf("%s: %v", zone, err)
		}
		w.loc.Store(loc)
	}
	if n, err := w.Write([]byte(msg + "\n")); err != nil || n != len(msg)+1 {
		t.Fatalf("n=%d err=%v", n, err)
	}
	return buf.String()
}

// No zone is what the log package printed before: UTC, in its LstdFlags shape.
func TestWithoutAZoneTheLineIsUTCInTheStandardLoggersFormat(t *testing.T) {
	at := time.Date(2026, 10, 19, 10, 0, 5, 0, time.UTC)
	if got := lineAt(t, "", at, "[clock] hello"); got != "2026/10/19 10:00:05 [clock] hello\n" {
		t.Fatalf("got %q", got)
	}
}

// The zone is the one named on either side of a DST change (Europe/Warsaw is
// +2 until 2026-10-25 and +1 after it), at a half-hour offset and at a
// quarter-hour one.
func TestLineFollowsTheNamedZone(t *testing.T) {
	for _, c := range []struct {
		zone string
		at   time.Time
		want string
	}{
		{"Europe/Warsaw", time.Date(2026, 10, 19, 10, 0, 0, 0, time.UTC), "2026/10/19 12:00:00"},
		{"Europe/Warsaw", time.Date(2026, 10, 26, 10, 0, 0, 0, time.UTC), "2026/10/26 11:00:00"},
		{"America/St_Johns", time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC), "2026/01/01 08:30:00"},
		{"Asia/Kathmandu", time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC), "2026/01/01 17:45:00"},
		{"Pacific/Auckland", time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC), "2026/01/02 01:00:00"},
	} {
		if got := lineAt(t, c.zone, c.at, "m"); got != c.want+" m\n" {
			t.Errorf("%s: %q, want %q", c.zone, got, c.want+" m\n")
		}
	}
}

func TestSetLogZoneAppliesToTheInstalledLogger(t *testing.T) {
	defer logZone.loc.Store(nil)
	if err := SetLogZone("Europe/Warsaw"); err != nil {
		t.Fatal(err)
	}
	if logZone.loc.Load() == nil {
		t.Fatal("zone not stored")
	}
}

func TestUnknownOrEmptyZoneLeavesTheLogWhereItWas(t *testing.T) {
	defer logZone.loc.Store(nil)
	if err := SetLogZone("Europe/Warsaw"); err != nil {
		t.Fatal(err)
	}
	before := logZone.loc.Load()
	if err := SetLogZone("Not/AZone"); err == nil {
		t.Fatal("an unknown zone was accepted")
	}
	if err := SetLogZone(""); err != nil {
		t.Fatalf("empty must be a no-op: %v", err)
	}
	if logZone.loc.Load() != before {
		t.Fatal("a refused or empty zone changed the log's zone")
	}
}

// Zone changes arrive on the control goroutine while every other goroutine logs.
func TestZoneWriterIsRaceFree(t *testing.T) {
	var buf bytes.Buffer
	w := &zoneWriter{out: &buf}
	done := make(chan struct{})
	go func() {
		loc, _ := time.LoadLocation("Europe/Warsaw")
		for i := 0; i < 500; i++ {
			w.loc.Store(loc)
		}
		close(done)
	}()
	for i := 0; i < 500; i++ {
		w.Write([]byte("x\n"))
	}
	<-done
}

func withZoneFile(t *testing.T) string {
	t.Helper()
	old := zonePath
	zonePath = filepath.Join(t.TempDir(), "etc", "timezone") // the directory does not exist yet
	t.Cleanup(func() { zonePath = old; logZone.loc.Store(nil) })
	logZone.loc.Store(nil)
	return zonePath
}

func TestAZoneTheControllerNamedSurvivesARestartWithNoController(t *testing.T) {
	path := withZoneFile(t)
	if err := ApplyControllerZone("Europe/Warsaw"); err != nil {
		t.Fatal(err)
	}
	if raw, _ := os.ReadFile(path); string(raw) != "Europe/Warsaw\n" {
		t.Fatalf("kept %q", raw)
	}
	logZone.loc.Store(nil) // the next boot, before any controller
	LoadLogZone()
	loc := logZone.loc.Load()
	if loc == nil || loc.String() != "Europe/Warsaw" {
		t.Fatalf("restored %v", loc)
	}
}

func TestAChangedZoneReplacesTheKeptOneAndAnUnchangedOneIsNotRewritten(t *testing.T) {
	path := withZoneFile(t)
	if err := ApplyControllerZone("Europe/Warsaw"); err != nil {
		t.Fatal(err)
	}
	first, _ := os.Stat(path)
	old := time.Now().Add(-time.Hour)
	os.Chtimes(path, old, old)
	if err := ApplyControllerZone("Europe/Warsaw"); err != nil {
		t.Fatal(err)
	}
	if again, _ := os.Stat(path); !again.ModTime().Before(first.ModTime()) {
		t.Fatal("an unchanged zone was written again")
	}
	if err := ApplyControllerZone("Asia/Tokyo"); err != nil {
		t.Fatal(err)
	}
	if raw, _ := os.ReadFile(path); string(raw) != "Asia/Tokyo\n" {
		t.Fatalf("kept %q", raw)
	}
	if _, err := os.Stat(path + ".tmp"); err == nil {
		t.Fatal("the temporary file was left behind")
	}
}

func TestNoControllerOpinionOrABadNameNeverEraseTheKeptZone(t *testing.T) {
	path := withZoneFile(t)
	if err := ApplyControllerZone("Europe/Warsaw"); err != nil {
		t.Fatal(err)
	}
	if err := ApplyControllerZone(""); err != nil {
		t.Fatal(err)
	}
	if err := ApplyControllerZone("Not/AZone"); err == nil {
		t.Fatal("a bad name was accepted")
	}
	if raw, _ := os.ReadFile(path); string(raw) != "Europe/Warsaw\n" {
		t.Fatalf("kept zone changed to %q", raw)
	}
}

func TestACorruptKeptZoneIsIgnoredNotFatal(t *testing.T) {
	path := withZoneFile(t)
	os.MkdirAll(filepath.Dir(path), 0o755)
	os.WriteFile(path, []byte("garbage\x00\n"), 0o644)
	LoadLogZone()
	if logZone.loc.Load() != nil {
		t.Fatal("a corrupt zone was applied")
	}
	if err := ApplyControllerZone("Europe/Warsaw"); err != nil { // and the controller can repair it
		t.Fatal(err)
	}
	if raw, _ := os.ReadFile(path); string(raw) != "Europe/Warsaw\n" {
		t.Fatalf("not repaired: %q", raw)
	}
}
