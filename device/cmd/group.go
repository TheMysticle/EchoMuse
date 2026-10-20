package main

import (
	"errors"
	"log"
	"sync"
	"sync/atomic"
	"time"

	"github.com/wilbowes/EchoMuse/internal/client"
	"github.com/wilbowes/EchoMuse/internal/config"
	"github.com/wilbowes/EchoMuse/internal/platform"
	"github.com/wilbowes/EchoMuse/internal/sendspin"
	"github.com/wilbowes/EchoMuse/internal/spotify"
)

// Speaker groups (internal/sendspin leader.go). One Echo leads: it plays its
// own Spotify and sends the same audio, stamped for a moment a little ahead,
// to the others, which are ordinary Sendspin players. The controller decides
// who leads and tells each Echo its part in the config push (groupRole,
// groupLead); nothing here is persisted, so a restart with no controller is a
// plain Echo.
//
// An Echo that was not told to lead has no Leader: groupLeader is nil, the
// music mux tests the pointer and does nothing else, and no goroutine, port
// or file exists on its account.

// groupStore holds the leader's own Sendspin identity and the keys of the
// players it leads. Beside sendspin.json, on /data, so it survives an OTA.
const groupStore = "/data/local/etc/echomuse/sendspin_leader.json"

// leader is read by the music mux on every period, without a lock.
var groupPtr atomic.Pointer[sendspin.Leader]

func groupLeader() *sendspin.Leader { return groupPtr.Load() }

var gl struct {
	mu        sync.Mutex
	l         *sendspin.Leader
	src       *spotify.Player // the engine the running leader reads
	suspended bool            // the controller link is down: stay stopped
	lastSent  bool            // a non-nil status has been reported
}

// groupWanted reads the config for a leader's orders.
func groupWanted() (*config.GroupLead, bool) {
	snap := config.Get().Snapshot()
	if snap.GroupRole == nil || *snap.GroupRole != config.RoleLeader || snap.GroupLead == nil {
		return nil, false
	}
	// The leader reads the Spotify engine's audio, which exists on emOS only.
	return snap.GroupLead, platform.Base() == platform.EmOS
}

// applyGroupConfig is called on a config push, which only happens with the
// controller link up.
func applyGroupConfig(cc *client.ControlClient, vol deviceVolume) {
	gl.mu.Lock()
	gl.suspended = false
	gl.mu.Unlock()
	reconcileGroup(cc, vol, true)
	// A controller that has just reconnected knows nothing of the group.
	gl.mu.Lock()
	defer gl.mu.Unlock()
	if gl.l != nil {
		gl.lastSent = true
		cc.SendGroupStatus(gl.l.Status())
	}
}

// reconcileGroup makes the running leader match the orders. Called on every
// config push (push true, which also brings the member list up to date) and
// from the 100ms poll, which is what starts a leader whose Spotify engine was
// not up yet when the order arrived. With no orders and no leader it is a lock
// and two comparisons.
func reconcileGroup(cc *client.ControlClient, vol deviceVolume, push bool) {
	lead, ok := groupWanted()
	sp := spotifyPlayer()

	gl.mu.Lock()
	defer gl.mu.Unlock()
	if gl.suspended {
		return
	}
	if !ok || lead == nil {
		if gl.l != nil {
			stopGroupLocked("no longer leading")
		}
		if gl.lastSent {
			gl.lastSent = false
			cc.SendGroupStatus(nil)
		}
		return
	}
	// A leader reads one engine. A new engine (a rename, an upgrade) is a new
	// source: restart rather than read a closed ring.
	if gl.l != nil && gl.src != sp {
		stopGroupLocked("the Spotify engine changed")
	}
	if gl.l == nil {
		if sp == nil {
			return // waits for the engine; the poll tries again
		}
		l, err := sendspin.NewLeader(sendspin.LeaderConfig{StorePath: groupStore, Name: lead.Name, Version: client.Version})
		if err != nil {
			log.Printf("[group] leader not started: %v", err)
			return
		}
		l.OnChange(debounce(300*time.Millisecond, func() { cc.SendGroupStatus(l.Status()) }))
		l.SetVolume(levelToPct(vol.VolumeLevel()))
		l.SetMembers(lead.Name, sendspin.ParseChannel(lead.Channel), groupMembers(lead))
		if err := l.Start(sp); err != nil {
			log.Printf("[group] leader not started: %v", err)
			l.Close()
			return
		}
		gl.l, gl.src = l, sp
		groupPtr.Store(l)
		log.Printf("[group] leading %q with %d member(s)", lead.Name, len(lead.Members))
	} else if push {
		gl.l.SetMembers(lead.Name, sendspin.ParseChannel(lead.Channel), groupMembers(lead))
	}
}

func groupMembers(lead *config.GroupLead) []sendspin.Member {
	ms := make([]sendspin.Member, 0, len(lead.Members))
	for _, m := range lead.Members {
		ms = append(ms, sendspin.Member{ID: m.ID, ClientID: m.ClientID, Host: m.Host, Port: m.Port,
			Channel: sendspin.ParseChannel(m.Channel)})
	}
	return ms
}

// stopGroupLocked ends the leader. The pointer goes first so the speaker
// stops asking it for audio; the engine's ring is then the speaker's again.
func stopGroupLocked(why string) {
	l := gl.l
	groupPtr.Store(nil)
	gl.l, gl.src = nil, nil
	if l != nil {
		l.Close()
		log.Printf("[group] leader stopped: %s", why)
	}
}

// groupLinkDown stops the leader when the controller link goes, for the
// reason the Sendspin player does: EchoMuse is one system, and a group is the
// controller's arrangement. It stays stopped until a config push arrives.
func groupLinkDown() {
	gl.mu.Lock()
	defer gl.mu.Unlock()
	gl.suspended = true
	if gl.l != nil {
		stopGroupLocked("controller link down")
	}
}

// groupPoll runs on the 100ms ticker.
func groupPoll(cc *client.ControlClient, vol deviceVolume) { reconcileGroup(cc, vol, false) }

// groupVolumeChanged passes the Echo's volume on to the Echoes it leads.
func groupVolumeChanged(level int) {
	if l := groupLeader(); l != nil {
		l.SetVolume(levelToPct(level))
	}
}

// groupKey installs a key the controller minted: a member learns its
// leader's id, a leader learns a member's.
func groupKey(kind, peer string, psk []byte) error {
	switch kind {
	case "trust":
		c := sendspinPlayer()
		if c == nil {
			return errors.New("this Echo's Sendspin player is not running yet")
		}
		return c.TrustServer(peer, psk)
	case "key":
		l := groupLeader()
		if l == nil {
			return errors.New("this Echo is not leading yet")
		}
		return l.SetKey(peer, psk)
	}
	return errors.New("unknown key kind")
}
