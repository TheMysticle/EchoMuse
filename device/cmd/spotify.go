package main

import (
	"fmt"
	"log"
	"os"
	"sync"
	"sync/atomic"
	"time"

	"github.com/wilbowes/EchoMuse/internal/bindings/speaker"
	"github.com/wilbowes/EchoMuse/internal/client"
	"github.com/wilbowes/EchoMuse/internal/config"
	"github.com/wilbowes/EchoMuse/internal/firewall"
	"github.com/wilbowes/EchoMuse/internal/platform"
	"github.com/wilbowes/EchoMuse/internal/server"
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
	mu       sync.Mutex
	p        *spotify.Player
	failed   *spotify.Player // start refused (engine missing); only its status is kept
	name     string
	stamp    string    // the engine binary the running process was started from
	waiting  bool      // enabled, but not started yet (wall clock not set)
	pausedAt time.Time // when the arbitration pause was last sent in the current takeover
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
	// While this Echo leads a group its audio is held in the leader, ahead of
	// the speaker, after the engine has stopped looking busy.
	spActive := func() bool { return spl != nil && (spl.Active() || groupActive()) }
	m.mu.Lock()
	defer m.mu.Unlock()
	switch m.owner {
	case ownSendspin:
		if ss != nil && ss.Active() {
			return true
		}
	case ownSpotify:
		if spActive() {
			return true
		}
	}
	m.owner = ownNone
	if ss != nil && ss.Active() {
		m.owner = ownSendspin
		return true
	}
	if spActive() {
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
		// A group leader places its audio in time, so it needs the measured
		// playAt that this mux passes through. Do not give the mux a
		// NeedsClock that says no.
		if g := groupLeader(); g != nil && g.Engaged() {
			return g.Fill(out, playAt, uncertain)
		}
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
	// Controllerless mode: the operator asked the Connect engine to keep
	// serving Spotify even when the control link is gone. Leave the process
	// alone; status pushes will fail quietly until the controller returns.
	snap := config.Get().Snapshot()
	if snap.SpotifyStandalone != nil && *snap.SpotifyStandalone &&
		snap.SpotifyEnabled != nil && *snap.SpotifyEnabled {
		log.Printf("[spotify] controller link down — engine kept running (standalone)")
		return
	}
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

// groupActive reports whether the leader has audio queued to play here.
func groupActive() bool {
	g := groupLeader()
	return g != nil && g.Active()
}

func spotifyClockSane() bool { return time.Now().Year() >= 2024 }

// spotifyBinaryStamp identifies the engine binary on disk. Empty when it
// cannot be read, which BinaryChanged treats as "no evidence of a change"
// rather than as a change.
//
// Size and mtime, not an md5: the controller already md5-verified the
// transfer, the install renames a fresh file into place so the mtime moves,
// and hashing 4MB on every config push is a cost with nothing to buy.
func spotifyBinaryStamp() string {
	fi, err := os.Stat(spotifyBinary)
	if err != nil {
		return ""
	}
	return fmt.Sprintf("%d:%d", fi.Size(), fi.ModTime().UnixNano())
}

// applySpotifyConfig starts, stops or renames the engine to match config.
// Like Sendspin it runs on a config push, so only with the link up, and a
// rename restarts it because the name is in the zeroconf record.
func applySpotifyConfig(spk *speaker.PcmSpeaker, cc *client.ControlClient, vol deviceVolume, deviceID string) {
	if platform.Base() != platform.EmOS {
		return
	}
	snap := config.Get().Snapshot()
	on := snap.SpotifyEnabled != nil && *snap.SpotifyEnabled
	standalone := snap.SpotifyStandalone != nil && *snap.SpotifyStandalone
	name := snap.SpotifyName
	if name == "" {
		name = snap.SendspinName
	}
	if name == "" {
		name = "Echo " + tail(deviceID, 4)
	}
	// Remember for a reboot that may not have a controller yet. Recorded on
	// every push so turning standalone OFF is remembered too; LoadSpotifyState
	// ignores the record unless standalone was on, which is what stops a plain
	// enable outliving the link.
	server.SaveSpotifyState(on, standalone, name)

	sp.mu.Lock()
	defer sp.mu.Unlock()
	// A newly installed engine has to be picked up, and installing the file
	// does not restart the process. The controller pushes the config again
	// after an install for exactly this; without the stamp check that push
	// is a no-op whenever the engine is already running, so the new binary
	// would sit on disk while the old one kept playing.
	upgraded := sp.p != nil && spotify.BinaryChanged(sp.stamp, spotifyBinaryStamp())
	if sp.p != nil && (!on || name != sp.name || upgraded) {
		sp.p.Stop()
		sp.p = nil
		spPresent.Store(false)
		installMusicMux(spk)
		if upgraded {
			log.Printf("[spotify] engine stopped: a new engine was installed")
		} else {
			log.Printf("[spotify] engine stopped (config)")
		}
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
	// Read per event, not captured here: a config change must take effect
	// without restarting the engine, which would cut the music to change a
	// ring setting.
	p.OnVolume(func(v uint16) {
		level := pctToLevel(u16ToPct(v))
		ring := true
		if r := config.Get().Snapshot().SpotifyVolumeRing; r != nil {
			ring = *r
		}
		vol.SetVolumeFrom(level, "spotify", ring)
	})
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
	sp.stamp = spotifyBinaryStamp()
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
	now := time.Now()
	pause := spotify.PauseDue(taken, p.Playing(), sp.pausedAt, now)
	first := pause && sp.pausedAt.IsZero()
	if pause {
		sp.pausedAt = now
	} else if !taken {
		sp.pausedAt = time.Time{}
	}
	sp.mu.Unlock()
	if pause {
		p.Send("pause", nil)
		p.Discard()
		if first {
			log.Printf("[spotify] paused: the music plane is taken")
		}
	}
}

func spotifyMusic() (active bool, lead time.Duration) {
	if g := groupLeader(); g != nil && g.Engaged() && g.Active() {
		return true, g.Buffered()
	}
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

// restoreSpotifyFromState starts the Connect engine from the last persisted
// standalone settings when the process boots with no controller yet. No-op
// unless SpotifyStandalone was on at the last config push.
func restoreSpotifyFromState(spk *speaker.PcmSpeaker, cc *client.ControlClient, dc *client.DataClient, srv *server.Server, deviceID string) {
	if platform.Base() != platform.EmOS {
		return
	}
	st, ok := server.LoadSpotifyState()
	if !ok || !st.Enabled {
		return
	}
	// Seed the in-memory config so applySpotifyConfig / link-down see the
	// same values the controller last pushed.
	enabled, standalone := true, true
	cfg := config.ConfigMessage{
		SpotifyEnabled:    &enabled,
		SpotifyStandalone: &standalone,
		SpotifyName:       st.Name,
	}
	if st.OwwOnDevice == config.OnDeviceOn {
		cfg.OwwOnDevice = st.OwwOnDevice
		cfg.OwwModel = st.OwwModel
	}
	config.Get().Apply(cfg)

	// Offline standalone mode: suppress orange disconnect pulse.
	if srv != nil {
		srv.SetPulseSuppressed(true)
		srv.SetLinkDown(true)
	}

	log.Printf("[spotify] restoring controllerless engine from saved state")
	applySpotifyConfig(spk, cc, srv, deviceID)

	if st.OwwOnDevice == config.OnDeviceOn && dc != nil {
		log.Printf("[spotify] restoring on-device wake word engine (%s) offline", st.OwwModel)
		applyShadowConfig(dc, cc, spk, srv)
		// Not while muted — mute was restored in NewServer, before this runs.
		if dc.ShadowScorer() != nil && (srv == nil || !srv.IsMuted()) {
			dc.EnsureOfflineMic()
		}
	}
}
