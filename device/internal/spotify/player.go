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

	// primePeriods is how much must be queued before reading resumes after a
	// gap. Three periods is ~64ms at the speaker's 1024-frame period — deep
	// enough that ordinary scheduling jitter cannot empty it, and far short
	// of the ring's 250ms, so the tail you hear after a pause is unchanged.
	primePeriods = 3

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
	aud      net.Conn // the audio connection that owns the ring
	cmdID    uint64
	onChange func()
	onVolume func(uint16)
	cancel   context.CancelFunc
	done     chan struct{}
	ctlLn    net.Listener
	audLn    net.Listener

	// What a speaker group leader needs to know about the audio, which it
	// reads ahead of the speaker (see Events). Guarded by mu.
	seenGen    uint64
	pausedMark bool   // the engine paused since Events was last called
	staleMark  bool   // queued audio belongs to a track that is no longer next
	pausedURI  string // the track a pause happened in, until it resumes

	// Fill's alone (one goroutine, the speaker's write loop): when it began
	// declining a tail the engine will not top up, and when it last ran.
	strandedAt, lastFill time.Time
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

// Buffered is how much audio is queued and not yet played.
func (p *Player) Buffered() time.Duration {
	return time.Duration(p.ring.len()) * time.Second / (48000 * 4)
}

// Discard drops queued audio, for when another source takes the plane.
func (p *Player) Discard() { p.ring.flush() }

// Playing reports whether the engine believes it is playing, for deciding
// whether an arbitration pause is needed.
func (p *Player) Playing() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.st.Engine == EngineRunning && (p.st.State == "playing" || p.st.State == "loading")
}

// Fill writes one period. A short read is zero-padded: a partial period
// only happens at the end of a stream, where the tail is silence anyway.
// NeedsClock is false: Fill ignores playAt entirely.
//
// Spotify audio is not placed in time against a server clock — it plays as
// it arrives, paced by the ring's backpressure. Saying so lets the speaker
// skip the DAC status read, which is a procfs read ON THE WRITE LOOP, once
// per period, and measured on a Radar at 199us typically but 7.8ms, 8.5ms
// and 14.7ms in its spikes (2026-10-11). The loop has 42.7ms to deliver a
// period and the hardware buffer is 85ms, so that read is a large share of
// a budget it buys this source nothing.
//
// It is also when the dropouts started. Before the pull sources existed,
// music arrived on the channel-fed 0x04 plane and the write loop did no
// procfs read at all — which is exactly the "it did not happen before
// librespot" the operator reported.
func (p *Player) NeedsClock() bool { return false }

func (p *Player) Fill(out []byte, _ time.Time, _ time.Duration) bool {
	// A WHOLE period or none. See ring.readPeriod: taking a partial one and
	// zero-padding it put silence in the middle of the waveform — a click no
	// counter could see, because the period itself was delivered on time and
	// full length.
	//
	// primePeriods of refill after a gap, so jitter around a period boundary
	// costs one gap rather than a run of them. Expressed in periods rather
	// than bytes so it follows whatever period the speaker asks for.
	if p.ring.readPeriod(out, primePeriods*len(out)) {
		p.strandedAt = time.Time{}
		return true
	}
	p.dropStrandedTail()
	return false
}

// strandedAfter is how long a tail that cannot be read, with the engine not
// playing, is kept before it is thrown away.
var strandedAfter = 2 * time.Second

// dropStrandedTail discards audio that can never play. A declined read leaves
// what is queued where it is, and reading resumes only once the ring refills
// to the prime. When the engine has stopped (another device took over, or the
// track ended) nothing refills it, yet Active reports any queued byte as
// audio: the speaker kept this source as the music owner for good, and a
// group member reported "playing something else" until the engine played
// something again (2026-10-22: Spotify played on a member, then on its group).
// Not at once: audio can arrive a moment before the engine's state event.
func (p *Player) dropStrandedTail() {
	now := time.Now()
	idle := now.Sub(p.lastFill) > time.Second
	p.lastFill = now
	if p.Playing() || p.ring.len() == 0 || idle {
		p.strandedAt = time.Time{}
		return
	}
	if p.strandedAt.IsZero() {
		p.strandedAt = now
		return
	}
	if now.Sub(p.strandedAt) >= strandedAfter {
		p.ring.flush()
		p.strandedAt = time.Time{}
	}
}

// ── Group leader ───────────────────────────────────────────────────────────
//
// When an Echo leads a speaker group (internal/sendspin Leader) the audio is
// read here by the leader, which stamps it and plays it on its own schedule,
// instead of being pulled by the speaker. These three methods are all it
// needs, and none of them runs unless a leader is reading.

// groupChunk is the leader's period: 20ms of 48kHz stereo S16LE.
const groupChunk = 960 * 4

// Read fills out with exactly len(out) bytes of audio, waiting up to wait.
func (p *Player) Read(out []byte, wait time.Duration) bool { return p.ring.readWait(out, wait) }

// Idle reports that nothing is queued and the engine is not playing. A tail
// shorter than one period (the end of a track) is not audio anybody will hear.
func (p *Player) Idle() bool { return p.ring.len() < groupChunk && !p.Playing() }

// Events reports, and forgets, that queued audio was thrown away (a skip, a
// stop, another source taking the speaker) or that the engine paused, since
// it was last called. Audio the leader has already sent ahead is stale after
// the first and is kept for resuming after the second.
// Paused is whether the engine is paused now. Audio that still arrives while
// it is true is the end of the pause, not playback starting.
func (p *Player) Paused() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.st.State == "paused"
}

func (p *Player) Events() (discarded, paused bool) {
	g := p.ring.gen.Load()
	p.mu.Lock()
	defer p.mu.Unlock()
	discarded = g != p.seenGen || p.staleMark
	p.seenGen, p.staleMark = g, false
	paused = p.pausedMark
	p.pausedMark = false
	return
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
		if s.State == "playing" && e.State == "paused" {
			p.pausedMark = true
			p.pausedURI = ""
			if s.Track != nil {
				p.pausedURI = s.Track.URI
			}
		}
		// Resumed into a different track: what a pause kept is not next.
		// The engine announces a track before it plays it, so the track is
		// already the new one here; the other order is caught below.
		if e.State == "playing" && p.pausedURI != "" {
			if s.Track != nil && s.Track.URI != p.pausedURI {
				p.staleMark = true
			}
			p.pausedURI = ""
		}
		s.State, s.PositionMs = e.State, e.PositionMs
	case "track":
		if p.pausedURI != "" && e.URI != p.pausedURI {
			p.staleMark = true
			p.pausedURI = ""
		}
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

// acceptAudio takes the newest audio connection and displaces the previous
// one, the same single-owner rule acceptControl has always had.
//
// It used to accept every connection and let them ALL write into one ring,
// with nothing ever displaced. That is only safe while exactly one engine
// can exist, and on 2026-10-11 two did on a Radar — an engine started
// 23:46:18 was still alive when another started 00:10:43, confirmed by pid.
// Both would have been streaming into this ring at once: two PCM streams
// interleaved at arbitrary byte offsets, which is not a glitch but garbage,
// and heard as crackling. Meanwhile control went only to the newest, so
// commands reached one engine while the other played — "it keeps playing
// and I cannot pause it".
//
// The ring is flushed on takeover: whatever the old engine left in it
// belongs to a different stream at a different position, so playing it out
// is a click by construction.
func (p *Player) acceptAudio(ln net.Listener) {
	for {
		c, err := ln.Accept()
		if err != nil {
			return
		}
		p.mu.Lock()
		old := p.aud
		p.aud = c
		p.mu.Unlock()
		if old != nil {
			old.Close()
			p.ring.flush()
			// Logged because the flush is AUDIBLE. Discarding the ring
			// mid-stream is a skip, and a skip with no trace is what made
			// the last four instruments useless: every one of them asked a
			// question that assumed the cause, and the answer came back
			// empty while the audio was plainly glitching.
			log.Printf("[spotify] audio socket taken over by a newer " +
				"connection — ring flushed, this is an audible skip")
		}
		go p.readAudio(c)
	}
}

func (p *Player) readAudio(c net.Conn) {
	defer func() {
		c.Close()
		p.mu.Lock()
		if p.aud == c {
			p.aud = nil
		}
		p.mu.Unlock()
	}()
	buf := make([]byte, 8192)
	for {
		n, err := c.Read(buf)
		// A displaced connection must stop writing. Checking ownership per
		// read rather than trusting Close to race us: the old engine can
		// have bytes already in flight.
		if n > 0 {
			p.mu.Lock()
			mine := p.aud == c
			p.mu.Unlock()
			if !mine {
				return
			}
			if !p.ring.write(buf[:n]) {
				return
			}
		}
		if err != nil {
			return
		}
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

// pauseRetry is how often the arbitration pause is sent again while the
// engine still plays under a taken music plane.
const pauseRetry = time.Second

// PauseDue says whether the engine should be sent the arbitration pause: it
// is playing while another source holds the music plane, and no pause went
// out in the last pauseRetry. One pause is not enough. A Spotify client
// choosing this Echo makes the engine load and play, but librespot ignores a
// pause until the Echo is the active device, so a pause sent at the first
// moment is lost and the engine decodes under the other source and starts
// playing by itself when that ends (2026-10-22, a group member).
func PauseDue(taken, playing bool, sentAt, now time.Time) bool {
	return taken && playing && (sentAt.IsZero() || now.Sub(sentAt) >= pauseRetry)
}
