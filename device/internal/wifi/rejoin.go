package wifi

import (
	"log"
	"strings"
	"sync"
	"time"
)

// An Echo can stop receiving multicast while it stays associated and every
// unicast path keeps working. Seen on a Radar under emOS, 2026-10-26: after a
// firmware restart its mDNS browse for the controller failed every round for
// 8 minutes while the controller answered each query within a second (seen
// from another host), the Echo's own Spotify engine answered no mDNS query,
// and the group rekeys since boot had all completed. `wpa_cli reassociate`
// (a fresh 4-way handshake and group key) and the next browse found the
// controller in a second; a power cut had been the only known fix.
//
// So a device that cannot find its controller by mDNS, and is associated,
// rejoins its network: after rejoinAfterMisses consecutive failed browses
// (~35s with discovery's 5/10/20s backoff), and then at most once per
// rejoinEvery, so a controller that is really gone costs a short WiFi blip
// every quarter of an hour and nothing else. emOS only: on FireOS the
// framework owns the supplicant, and a hand-driven reconnect there is the
// path the package comment forbids.
const (
	rejoinAfterMisses = 3
	rejoinEvery       = 15 * time.Minute
)

var rejoin struct {
	mu   sync.Mutex
	last time.Time
}

// Hooks for tests.
var (
	rejoinNow        = time.Now
	rejoinOnEmOS     = onEmOS
	rejoinAssociated = associated
	rejoinRun        = func() (string, error) { return wpaCli("reassociate") }
)

// changing reports a WiFi change in flight: it owns the supplicant, and its
// own gates decide whether the new network works.
func changing() bool {
	mu.Lock()
	defer mu.Unlock()
	return inFlight
}

// RejoinIfDeaf is told how many browses in a row found no controller. It
// reports whether it asked the supplicant to reassociate.
func RejoinIfDeaf(misses int) bool {
	if misses < rejoinAfterMisses || !rejoinOnEmOS() || changing() {
		return false
	}
	rejoin.mu.Lock()
	defer rejoin.mu.Unlock()
	now := rejoinNow()
	if !rejoin.last.IsZero() && now.Sub(rejoin.last) < rejoinEvery {
		return false
	}
	if !rejoinAssociated() {
		return false // not a multicast problem; the supplicant is already at work
	}
	rejoin.last = now
	out, err := rejoinRun()
	if err != nil || !strings.Contains(out, "OK") {
		log.Printf("[wifi] reassociate failed: %v %q", err, strings.TrimSpace(out))
		return false
	}
	log.Printf("[wifi] no controller by mDNS after %d tries while associated: reassociating "+
		"(an Echo can stop hearing multicast until it rejoins)", misses)
	return true
}
