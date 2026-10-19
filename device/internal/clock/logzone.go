package clock

import (
	"io"
	"log"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"time"
	_ "time/tzdata" // an Echo has no zoneinfo of its own, so the zone database travels in the binary
)

// The log's timestamps are in the zone the controller names, not the device's.
//
// An Echo has no timezone: Go finds none and prints UTC, so server.log read two
// hours behind the room it was in (CEST, 2026-10-19) and every line had to be
// converted by hand against the controller's log. The controller names its own
// zone on the ack beside time_ms; here that zone dresses the timestamp.
//
// The timestamp is written by this writer instead of the log package, and the
// zone is held in an atomic pointer, because the alternative is assigning
// time.Local while other goroutines call time.Now: a data race by the standard
// library's own account. Nothing but the log reads this zone — alert schedules
// carry their own (internal/schedule), and every measurement is monotonic.
//
// The zone is kept in a file, so the next boot starts in it: a controller that
// is down, or never reachable, must not turn the log back into UTC. A zone is
// written only when the controller names a different one, so a reconnect costs
// a comparison and not a flash write. The Echo does not look its zone up
// itself: that would be an IP-geolocation request to a third party on every
// boot, a dependency and a leak of where the device is, for a label the
// controller already knows.
//
// Without a zone at all (an older controller that was never reached) the writer
// stamps UTC, which is what the log package printed before.
type zoneWriter struct {
	out io.Writer
	loc atomic.Pointer[time.Location]
	now func() time.Time // time.Now unless a test fixes it
}

var logZone zoneWriter

const logLayout = "2006/01/02 15:04:05 "

func (w *zoneWriter) Write(p []byte) (int, error) {
	loc := w.loc.Load()
	if loc == nil {
		loc = time.UTC
	}
	// One Write per entry, so the prefix and the line cannot be split apart.
	now := time.Now
	if w.now != nil {
		now = w.now
	}
	buf := now().In(loc).AppendFormat(make([]byte, 0, len(logLayout)+len(p)), logLayout)
	buf = append(buf, p...)
	if _, err := w.out.Write(buf); err != nil {
		return 0, err
	}
	return len(p), nil
}

// InstallLogOutput sends the standard logger to out with timestamps in the zone
// set by SetLogZone (UTC until then). The format is the log package's own
// LstdFlags, so nothing that reads server.log changes.
func InstallLogOutput(out io.Writer) {
	logZone.out = out
	log.SetFlags(0)
	log.SetOutput(&logZone)
}

// SetLogZone applies an IANA zone name to the log's timestamps. An empty name
// leaves things as they are; an unknown one is refused and reported, and the
// log stays on the zone it had.
func SetLogZone(name string) error {
	if name == "" {
		return nil
	}
	loc, err := time.LoadLocation(name)
	if err != nil {
		return err
	}
	logZone.loc.Store(loc)
	return nil
}

// zonePath is where the zone is kept. A variable so tests can point it away
// from the device's /data.
var zonePath = "/data/local/etc/echomuse/timezone"

// LoadLogZone restores the zone kept by an earlier boot. Absent, empty or
// unreadable all mean "none yet", and a stored name the zone database cannot
// load is reported and ignored.
func LoadLogZone() {
	raw, err := os.ReadFile(zonePath)
	if err != nil {
		return
	}
	if err := SetLogZone(strings.TrimSpace(string(raw))); err != nil {
		log.Printf("[clock] ignoring the stored timezone: %v", err)
	}
}

// ApplyControllerZone uses the zone the controller named and keeps it for the
// next boot, writing only when it differs from what is kept. Empty is the
// controller having no opinion and changes nothing. A failure to keep the zone
// leaves it applied for this run.
func ApplyControllerZone(name string) error {
	if name == "" {
		return nil
	}
	if err := SetLogZone(name); err != nil {
		return err
	}
	if kept, err := os.ReadFile(zonePath); err == nil && strings.TrimSpace(string(kept)) == name {
		return nil
	}
	// Write beside, then rename: a power cut leaves the old zone or the new
	// one, never half of a name.
	tmp := zonePath + ".tmp"
	if err := os.MkdirAll(filepath.Dir(zonePath), 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(tmp, []byte(name+"\n"), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, zonePath)
}
