package main

import (
	"log"
	"sync"
	"sync/atomic"
	"time"

	"github.com/wilbowes/EchoMuse/internal/bindings/speaker"
	"github.com/wilbowes/EchoMuse/internal/client"
	"github.com/wilbowes/EchoMuse/internal/config"
	"github.com/wilbowes/EchoMuse/internal/firewall"
	"github.com/wilbowes/EchoMuse/internal/platform"
	"github.com/wilbowes/EchoMuse/internal/spotify"
)

// The Spotify Connect engine's lifecycle (internal/spotify). emOS only, off
// by default, and it shares the one music slot on the speaker with Sendspin
// through musicMux: whichever is audible first keeps the plane, and the other
// is told it is taken. Home Assistant's own music (0x04) still beats both.
const (
	spotifyBinary   = "/data/local/share/echomuse/spotify/em-spotify"
	spotifyCacheDir = "/data/local/etc/echomuse/spotify"
	spotifySockDir  = "/data/local/tmp/em-spotify"
)

// Which producers exist, read by installMusicMux without taking either
// player's lock (Sendspin and Spotify each call it while holding their own).
var ssPresent, spPresent atomic.Bool

var sp struct {
	mu      sync.Mutex
	p       *spotify.Player
	failed  *spotify.Player // start refused (engine missing); only its status is kept
	name    string
	waiting bool // enabled, but not started yet (wall clock not set)
	paused  bool // arbitration pause already sent for the current takeover
}

func spotifyPlayer() *spotify.Player {
	sp.mu.Lock()
	defer sp.mu.Unlock()
	return sp.p
}

// musicMux gives the speaker's single music slot to one producer at a time.
// The owner is sticky until it goes quiet, so a second source starting
// mid-song cannot cut the first off.
type musicMux struct {
	mu    sync.Mutex
	owner int
}

var mux musicMux

const (
	ownNone = iota
	ownSendspin
	ownSpotify
)

func (m *musicMux) Active() bool {
	ss := sendspinPlayer()
	spl := spotifyPlayer()
	m.mu.Lock()
	defer m.mu.Unlock()
	switch m.owner {
	case ownSendspin:
		if ss != nil && ss.Active() {
			return true
		}
	case ownSpotify:
		if spl != nil && spl.Active() {
			return true
		}
	}
	m.owner = ownNone
	if ss != nil && ss.Active() {
		m.owner = ownSendspin
		return true
	}
	if spl != nil && spl.Active() {
		m.owner = ownSpotify
		return true
	}
	return false
}

func (m *musicMux) Fill(out []byte, playAt time.Time, uncertain time.Duration) bool {
	switch m.ownerNow() {
	case ownSendspin:
		if c := sendspinPlayer(); c != nil {
			return c.Fill(out, playAt, uncertain)
		}
	case ownSpotify:
		if p := spotifyPlayer(); p != nil {
			return p.Fill(out, playAt, uncertain)
		}
	}
	return false
}

func (m *musicMux) ownerNow() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.owner
}

// installMusicMux registers the mux as the speaker's music source while at
// least one producer exists.
func installMusicMux(spk *speaker.PcmSpeaker) {
	if ssPresent.Load() || spPresent.Load() {
		spk.SetMusicSource(&mux)
	} else {
		spk.SetMusicSource(nil)
	}
}

// Spotify's u16 volume against the Echo's 0-100 value. Both directions
// round and are a fixed point — see internal/spotify/proto.go for why that
// is load-bearing rather than tidy.
func pctToU16(pct int) uint16 { return spotify.PercentToU16(pct) }
func u16ToPct(v uint16) int   { return spotify.U16ToPercent(v) }

func spotifyVolumeChanged(level int) {
	if p := spotifyPlayer(); p != nil {
		_ = p.SetVolume(pctToU16(levelToPct(level)))
	}
}

func spotifyLinkDown(spk *speaker.PcmSpeaker) {
	sp.mu.Lock()
	defer sp.mu.Unlock()
	sp.waiting = false
	sp.failed = nil
	if sp.p == nil {
		return
	}
	sp.p.Stop()
	sp.p = nil
	spPresent.Store(false)
	installMusicMux(spk)
	firewall.Close(spotify.DefaultZeroconfPort)
	firewall.CloseUDP(spotify.MDNSPort)
	log.Printf("[spotify] engine stopped: controller link down")
}

func spotifyClockSane() bool { return time.Now().Year() >= 2024 }

// applySpotifyConfig starts, stops or renames the engine to match config.
// Like Sendspin it runs on a config push, so only with the link up, and a
// rename restarts it because the name is in the zeroconf record.
func applySpotifyConfig(spk *speaker.PcmSpeaker, cc *client.ControlClient, vol deviceVolume, deviceID string) {
	if platform.Base() != platform.EmOS {
		return
	}
	snap := config.Get().Snapshot()
	on := snap.SpotifyEnabled != nil && *snap.SpotifyEnabled
	name := snap.SpotifyName
	if name == "" {
		name = snap.SendspinName
	}
	if name == "" {
		name = "Echo " + tail(deviceID, 4)
	}

	sp.mu.Lock()
	defer sp.mu.Unlock()
	if sp.p != nil && (!on || name != sp.name) {
		sp.p.Stop()
		sp.p = nil
		spPresent.Store(false)
		installMusicMux(spk)
		log.Printf("[spotify] engine stopped (config)")
	}
	sp.waiting = on
	if !on || name != sp.name {
		sp.failed = nil
	}
	if !on {
		firewall.Close(spotify.DefaultZeroconfPort)
		firewall.CloseUDP(spotify.MDNSPort)
		cc.SendSpotifyStatus(nil)
		return
	}
	if sp.p == nil {
		sp.name = name
		startSpotifyLocked(spk, cc, vol, deviceID)
	}
}

// startSpotifyLocked starts the engine if the wall clock is usable: TLS to
// Spotify fails on an unset clock, and the failure would read as a bad
// account. Until then spotifyPoll retries.
func startSpotifyLocked(spk *speaker.PcmSpeaker, cc *client.ControlClient, vol deviceVolume, deviceID string) {
	if !spotifyClockSane() {
		return
	}
	p := spotify.New(spotify.Config{
		Binary:       spotifyBinary,
		CacheDir:     spotifyCacheDir,
		SocketDir:    spotifySockDir,
		Name:         sp.name,
		DeviceID:     deviceID,
		ZeroconfPort: spotify.DefaultZeroconfPort,
		Volume:       pctToU16(levelToPct(vol.VolumeLevel())),
	})
	p.OnVolume(func(v uint16) { vol.SetVolume(pctToLevel(u16ToPct(v))) })
	p.OnChange(debounce(300*time.Millisecond, func() { cc.SendSpotifyStatus(p.Status()) }))
	if ferr := firewall.Open(spotify.DefaultZeroconfPort); ferr != nil {
		log.Printf("[spotify] could not open port %d: %v", spotify.DefaultZeroconfPort, ferr)
	}
	if ferr := firewall.OpenUDP(spotify.MDNSPort); ferr != nil {
		log.Printf("[spotify] could not open udp %d: %v", spotify.MDNSPort, ferr)
	}
	if err := p.Start(); err != nil {
		firewall.Close(spotify.DefaultZeroconfPort)
		firewall.CloseUDP(spotify.MDNSPort)
		log.Printf("[spotify] engine not started: %v", err)
		// The status carries "missing" so the dashboard can offer the install.
		sp.waiting = false
		sp.failed = p
		cc.SendSpotifyStatus(p.Status())
		return
	}
	sp.p = p
	sp.failed = nil
	spPresent.Store(true)
	installMusicMux(spk)
	cc.SendSpotifyStatus(p.Status())
}

// spotifyPoll runs on the 100ms ticker: it retries a start that waited for
// the clock, and arbitrates against Sendspin and Home Assistant's music.
func spotifyPoll(spk *speaker.PcmSpeaker, cc *client.ControlClient, vol deviceVolume, deviceID string) {
	sp.mu.Lock()
	if sp.waiting && sp.p == nil {
		startSpotifyLocked(spk, cc, vol, deviceID)
	}
	p := sp.p
	sp.mu.Unlock()
	if p == nil {
		return
	}
	taken := spk.MusicPlaneBusy() || mux.ownerNow() == ownSendspin
	sp.mu.Lock()
	pause := taken && p.Playing() && !sp.paused
	if pause {
		sp.paused = true
	} else if !taken {
		sp.paused = false
	}
	sp.mu.Unlock()
	if pause {
		p.Send("pause", nil)
		p.Discard()
		log.Printf("[spotify] paused: the music plane is taken")
	}
}

func spotifyMusic() (active bool, lead time.Duration) {
	if p := spotifyPlayer(); p != nil && p.Active() {
		return true, p.Buffered()
	}
	return false, 0
}

func spotifyStatus() any {
	sp.mu.Lock()
	p, failed := sp.p, sp.failed
	sp.mu.Unlock()
	if p != nil {
		return p.Status()
	}
	if failed != nil {
		return failed.Status()
	}
	return nil
}

// spotifyCmd runs a controller command against the engine and returns the
// reply for the dashboard. The command set is the engine's own.
func spotifyCmd(cmd string, fields map[string]any) map[string]any {
	p := spotifyPlayer()
	if p == nil {
		return map[string]any{"error": "spotify engine is not running"}
	}
	if err := p.Send(cmd, fields); err != nil {
		return map[string]any{"error": err.Error()}
	}
	return map[string]any{"ok": true}
}

func spotifyShutdown() {
	if p := spotifyPlayer(); p != nil {
		p.Stop()
	}
}
