// Package spotify runs the on-device Spotify Connect engine (device/spotify,
// Rust, librespot) and bridges it to the speaker.
//
// The engine is a separate static binary, supervised here. It reaches the
// firmware over two unix sockets that THIS side listens on, so the sockets
// exist before the engine does and a restarted engine just redials:
//
//	control: newline JSON, commands in and events out (proto.go)
//	audio:   bare 48kHz S16LE stereo, no framing, into ring
//
// Player is a speaker.MusicSource. Fill ignores the playAt it is offered:
// Spotify audio is not placed in time against a server clock, it is played
// as it arrives, with the ring's backpressure pacing the decoder.
package spotify

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	// DefaultZeroconfPort is where the engine serves zeroconf linking. Fixed,
	// because emOS drops inbound traffic until a rule admits the port.
	DefaultZeroconfPort = 57621
	// MDNSPort is the UDP port clients query Connect speakers on.
	MDNSPort = 5353

	// ringBytes is 250ms of 48kHz stereo S16LE. Whatever is queued plays
	// after a pause, so this is also the tail you hear.
	ringBytes = 48000

	restartMin = 2 * time.Second
	restartMax = 60 * time.Second
	// healthyRun is how long a process must last for its death not to count
	// against the backoff.
	healthyRun = 30 * time.Second
)

// Config is what Start needs. Binary and CacheDir are on the data
// partition; SocketDir is RAM-backed.
type Config struct {
	Binary       string
	CacheDir     string
	SocketDir    string
	Name         string
	DeviceID     string
	ZeroconfPort int
	Bitrate      int
	Volume       uint16 // initial, 0..65535
}

// Engine states, as reported. "running" only means the process is alive:
// whether the account is linked is Link, which the engine itself reports.
const (
	EngineStopped = "stopped"
	EngineMissing = "missing"
	EngineRunning = "running"
	EngineBackoff = "restarting"
)

// Status is everything the controller is told. Link is empty until the
// engine has said, and empty is shown as unknown rather than as anything
// reassuring.
type Status struct {
	Engine     string  `json:"engine"`
	Link       string  `json:"link,omitempty"`
	LinkDetail string  `json:"linkDetail,omitempty"`
	Connected  bool    `json:"connected"`
	User       string  `json:"user,omitempty"`
	State      string  `json:"state,omitempty"`
	PositionMs *uint32 `json:"positionMs,omitempty"`
	Track      *Track  `json:"track,omitempty"`
	Volume     *uint16 `json:"volume,omitempty"`
	Shuffle    bool    `json:"shuffle,omitempty"`
	Repeat     bool    `json:"repeat,omitempty"`
	RepeatOne  bool    `json:"repeatTrack,omitempty"`
	LastError  string  `json:"lastError,omitempty"`
	Restarts   int     `json:"restarts,omitempty"`
	Pid        int     `json:"pid,omitempty"`
}

type Player struct {
	cfg  Config
	ring *ring

	mu       sync.Mutex
	st       Status
	ctl      net.Conn
	cmdID    uint64
	onChange func()
	onVolume func(uint16)
	cancel   context.CancelFunc
	done     chan struct{}
	ctlLn    net.Listener
	audLn    net.Listener
}

func New(cfg Config) *Player {
	if cfg.ZeroconfPort == 0 {
		cfg.ZeroconfPort = DefaultZeroconfPort
	}
	if cfg.Bitrate == 0 {
		cfg.Bitrate = 160
	}
	return &Player{cfg: cfg, ring: newRing(ringBytes), st: Status{Engine: EngineStopped}}
}

func (p *Player) OnChange(f func())       { p.mu.Lock(); p.onChange = f; p.mu.Unlock() }
func (p *Player) OnVolume(f func(uint16)) { p.mu.Lock(); p.onVolume = f; p.mu.Unlock() }

func (p *Player) Status() Status {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.st
}

func (p *Player) changed() {
	p.mu.Lock()
	f := p.onChange
	p.mu.Unlock()
	if f != nil {
		f()
	}
}

func (p *Player) set(f func(*Status)) {
	p.mu.Lock()
	f(&p.st)
	p.mu.Unlock()
	p.changed()
}

// Start listens on the sockets and supervises the engine until Stop. A
// missing binary is reported and returned, not retried: nothing about
// waiting installs it.
func (p *Player) Start() error {
	if _, err := os.Stat(p.cfg.Binary); err != nil {
		p.set(func(s *Status) { s.Engine = EngineMissing; s.LastError = "engine not installed on this device" })
		return fmt.Errorf("spotify engine not installed: %w", err)
	}
	if err := os.MkdirAll(p.cfg.SocketDir, 0o700); err != nil {
		return err
	}
	if err := os.MkdirAll(p.cfg.CacheDir, 0o700); err != nil {
		return err
	}
	ctl, err := listenUnix(filepath.Join(p.cfg.SocketDir, "control.sock"))
	if err != nil {
		return err
	}
	aud, err := listenUnix(filepath.Join(p.cfg.SocketDir, "audio.sock"))
	if err != nil {
		ctl.Close()
		return err
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	p.mu.Lock()
	p.ctlLn, p.audLn, p.cancel, p.done = ctl, aud, cancel, done
	p.st = Status{Engine: EngineBackoff}
	p.mu.Unlock()
	p.ring.reopen()
	go p.acceptControl(ctl)
	go p.acceptAudio(aud)
	go func() {
		defer close(done)
		p.supervise(ctx)
	}()
	return nil
}

// Stop ends the engine and closes the sockets. Safe to call when not
// started.
func (p *Player) Stop() {
	p.mu.Lock()
	cancel, done := p.cancel, p.done
	p.cancel, p.done = nil, nil
	ctl, aud, conn := p.ctlLn, p.audLn, p.ctl
	p.ctlLn, p.audLn, p.ctl = nil, nil, nil
	p.mu.Unlock()
	if cancel == nil {
		return
	}
	cancel()
	if ctl != nil {
		ctl.Close()
	}
	if aud != nil {
		aud.Close()
	}
	if conn != nil {
		conn.Close()
	}
	p.ring.close()
	<-done
	p.ring.flush()
	p.set(func(s *Status) { *s = Status{Engine: EngineStopped} })
}

func listenUnix(path string) (net.Listener, error) {
	_ = os.Remove(path)
	ln, err := net.Listen("unix", path)
	if err != nil {
		return nil, err
	}
	_ = os.Chmod(path, 0o600)
	return ln, nil
}

// ── MusicSource ────────────────────────────────────────────────────────────

// Active reports whether the engine has the plane: audio queued, or a track
// playing or loading. The engine can be between tracks with an empty ring
// and state still playing, which must not read as idle.
func (p *Player) Active() bool {
	if p.ring.len() > 0 {
		return true
	}
	return p.Playing()
}

// Playing reports whether the engine believes it is playing, for deciding
// whether an arbitration pause is needed.
func (p *Player) Playing() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.st.Engine == EngineRunning && (p.st.State == "playing" || p.st.State == "loading")
}

// Fill writes one period. A short read is zero-padded: a partial period
// only happens at the end of a stream, where the tail is silence anyway.
func (p *Player) Fill(out []byte, _ time.Time, _ time.Duration) bool {
	n := p.ring.read(out)
	if n == 0 {
		return false
	}
	clear(out[n:])
	return true
}

// ── Commands ───────────────────────────────────────────────────────────────

var errNoEngine = errors.New("the Spotify engine is not connected")

// Send writes one command, with fields merged into the object.
func (p *Player) Send(cmd string, fields map[string]any) error {
	switch cmd {
	case "load", "load_tracks", "next", "prev", "stop", "disconnect", "logout":
		// New content (or none) is coming; what is queued belongs to the old.
		p.ring.flush()
	}
	p.mu.Lock()
	conn := p.ctl
	p.cmdID++
	id := p.cmdID
	p.mu.Unlock()
	if conn == nil {
		return errNoEngine
	}
	obj := map[string]any{"id": id, "cmd": cmd}
	for k, v := range fields {
		obj[k] = v
	}
	b, err := json.Marshal(obj)
	if err != nil {
		return err
	}
	_ = conn.SetWriteDeadline(time.Now().Add(2 * time.Second))
	_, err = conn.Write(append(b, '\n'))
	return err
}

// SetVolume tells the engine the device's volume moved. The engine does not
// echo it back, so this cannot loop.
func (p *Player) SetVolume(level uint16) error {
	p.set(func(s *Status) { v := level; s.Volume = &v })
	return p.Send("volume", map[string]any{"level": level})
}

// ── Sockets ────────────────────────────────────────────────────────────────

func (p *Player) acceptControl(ln net.Listener) {
	for {
		c, err := ln.Accept()
		if err != nil {
			return
		}
		p.mu.Lock()
		old := p.ctl
		p.ctl = c
		p.mu.Unlock()
		if old != nil {
			old.Close()
		}
		go p.readEvents(c)
	}
}

func (p *Player) readEvents(c net.Conn) {
	defer func() {
		c.Close()
		p.mu.Lock()
		if p.ctl == c {
			p.ctl = nil
		}
		p.mu.Unlock()
	}()
	// Bring the engine up to the device's volume before anything plays.
	p.mu.Lock()
	v := p.st.Volume
	p.mu.Unlock()
	if v != nil {
		_ = p.Send("volume", map[string]any{"level": *v})
	}
	sc := bufio.NewScanner(c)
	sc.Buffer(make([]byte, 0, 64<<10), 1<<20)
	for sc.Scan() {
		ev, rp := classify(sc.Bytes())
		switch {
		case ev != nil:
			p.apply(ev)
		case rp != nil && rp.Reply == "error":
			log.Printf("[spotify] engine refused command %d: %s", rp.ID, rp.Message)
		}
	}
}

func (p *Player) apply(e *event) {
	var vol *uint16
	p.mu.Lock()
	s := &p.st
	switch e.Event {
	case "state":
		s.State, s.PositionMs = e.State, e.PositionMs
	case "track":
		s.Track = &Track{URI: e.URI, Name: e.Name, Artists: e.Artists, Album: e.Album,
			DurationMs: e.DurationMs, Explicit: e.Explicit, CoverURL: e.CoverURL}
	case "volume":
		if e.Level != nil {
			l := *e.Level
			s.Volume, vol = &l, &l
		}
	case "session":
		s.Connected, s.User = e.Connected, e.User
	case "link":
		s.Link, s.LinkDetail = e.Status, e.Detail
		if e.Status != "linked" {
			s.Connected = false
		}
	case "shuffle":
		s.Shuffle = e.On
	case "repeat":
		s.Repeat, s.RepeatOne = e.Context, e.Track
	case "error":
		s.LastError = e.Kind + ": " + e.Message
	}
	onVol := p.onVolume
	p.mu.Unlock()
	p.changed()
	if vol != nil && onVol != nil {
		onVol(*vol)
	}
}

func (p *Player) acceptAudio(ln net.Listener) {
	for {
		c, err := ln.Accept()
		if err != nil {
			return
		}
		go func() {
			defer c.Close()
			buf := make([]byte, 8192)
			for {
				n, err := c.Read(buf)
				if n > 0 && !p.ring.write(buf[:n]) {
					return
				}
				if err != nil {
					return
				}
			}
		}()
	}
}

// ── Supervision ────────────────────────────────────────────────────────────

// premiumLine matches what librespot prints before it exits for a free
// account. The engine reports the same thing cleanly when it gets there
// first; this is for when librespot exits before it can.
func premiumLine(line string) bool {
	l := strings.ToLower(line)
	return strings.Contains(l, "free user") || strings.Contains(l, "free account") ||
		(strings.Contains(l, "premium") && strings.Contains(l, "not supported"))
}

func (p *Player) args() []string {
	a := []string{
		"--name", p.cfg.Name,
		"--device-id", p.cfg.DeviceID,
		"--cache-dir", p.cfg.CacheDir,
		"--control-socket", filepath.Join(p.cfg.SocketDir, "control.sock"),
		"--audio-socket", filepath.Join(p.cfg.SocketDir, "audio.sock"),
		"--zeroconf-port", strconv.Itoa(p.cfg.ZeroconfPort),
		"--bitrate", strconv.Itoa(p.cfg.Bitrate),
	}
	p.mu.Lock()
	v := p.st.Volume
	p.mu.Unlock()
	if v != nil {
		a = append(a, "--initial-volume", strconv.Itoa(int(*v)))
	} else if p.cfg.Volume > 0 {
		a = append(a, "--initial-volume", strconv.Itoa(int(p.cfg.Volume)))
	}
	return a
}

func (p *Player) supervise(ctx context.Context) {
	delay := restartMin
	for ctx.Err() == nil {
		started := time.Now()
		premium := p.runOnce(ctx)
		if ctx.Err() != nil {
			return
		}
		if premium {
			p.set(func(s *Status) {
				s.Engine, s.Link, s.Connected = EngineStopped, "premium_required", false
				s.LinkDetail = "Spotify Premium is required to play on a Connect speaker"
			})
			log.Printf("[spotify] account is not Premium; engine stays stopped until it is relinked")
			<-ctx.Done()
			return
		}
		if time.Since(started) >= healthyRun {
			delay = restartMin
		}
		p.set(func(s *Status) { s.Engine = EngineBackoff; s.Restarts++; s.Connected = false; s.State = "stopped" })
		log.Printf("[spotify] engine exited; restarting in %v", delay)
		select {
		case <-ctx.Done():
			return
		case <-time.After(delay):
		}
		delay = min(delay*2, restartMax)
	}
}

// runOnce runs the engine to its end and reports whether it died of a free
// account.
func (p *Player) runOnce(ctx context.Context) (premium bool) {
	cmd := exec.CommandContext(ctx, p.cfg.Binary, p.args()...)
	cmd.Cancel = func() error { return cmd.Process.Signal(syscall.SIGTERM) }
	cmd.WaitDelay = 3 * time.Second
	stderr, err := cmd.StderrPipe()
	if err != nil {
		p.set(func(s *Status) { s.Engine, s.LastError = EngineBackoff, err.Error() })
		return false
	}
	if err := cmd.Start(); err != nil {
		log.Printf("[spotify] could not start engine: %v", err)
		p.set(func(s *Status) { s.Engine, s.LastError = EngineBackoff, "could not start: "+err.Error() })
		return false
	}
	log.Printf("[spotify] engine started (pid %d)", cmd.Process.Pid)
	p.set(func(s *Status) { s.Engine, s.LastError, s.Pid = EngineRunning, "", cmd.Process.Pid })
	sc := bufio.NewScanner(stderr)
	for sc.Scan() {
		line := sc.Text()
		log.Printf("[spotify] %s", line)
		if premiumLine(line) {
			premium = true
		}
	}
	if err := cmd.Wait(); err != nil && ctx.Err() == nil {
		p.set(func(s *Status) { s.LastError = "engine exited: " + err.Error() })
	}
	return premium
}
