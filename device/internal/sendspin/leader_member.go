package sendspin

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"strconv"
	"sync"
	"time"

	"github.com/flynn/noise"
	"github.com/gorilla/websocket"
)

// Member states, as reported.
const (
	StateConnecting  = "connecting"
	StateReady       = "ready"
	StateNeedsKey    = "needs_key"
	StateBusy        = "busy"
	StateUnreachable = "unreachable"
	StateError       = "error"
)

const (
	dialTimeout  = 5 * time.Second
	backoffMin   = time.Second
	backoffMax   = 15 * time.Second
	busyBackoff  = 5 * time.Second
	authFailsMax = 2 // consecutive handshake rejections before the key is called bad
	// A player asks for the time at least every 3s once activated.
	leaderIdleTimeout = 30 * time.Second
	leaderWriteWait   = 10 * time.Second
)

type outKind int

const (
	outAudio outKind = iota
	outStart
	outClear
	outVolume
)

type outMsg struct {
	kind outKind
	ts   int64
	data []byte
	vol  int
}

// member is one led player and the goroutine that keeps a connection to it.
// l.mu guards cfg, ready and sentVol (the pump reads them on every chunk);
// mu guards the rest.
type member struct {
	l       *Leader
	cfg     Member
	ready   bool
	sentVol int

	qmu      sync.Mutex
	items    []outMsg
	notify   chan struct{}
	overflow bool

	mu         sync.Mutex
	state      string
	detail     string
	servedOnce bool               // this connection reached ready
	connStop   context.CancelFunc // ends the current connection only
	stopped    bool
	cancel     context.CancelFunc // ends the member
	done       chan struct{}
	wake       chan struct{}
}

func newMember(l *Leader, cfg Member) *member {
	return &member{
		l: l, cfg: cfg, sentVol: -1,
		notify: make(chan struct{}, 1),
		wake:   make(chan struct{}, 1),
		state:  StateConnecting,
		done:   make(chan struct{}),
	}
}

func (m *member) start() {
	ctx, cancel := context.WithCancel(context.Background())
	m.mu.Lock()
	m.cancel = cancel
	m.mu.Unlock()
	go func() {
		defer close(m.done)
		m.loop(ctx)
	}()
}

// shutdown ends the member and waits for its goroutine. The leader's lock
// must not be held.
func (m *member) shutdown() {
	m.mu.Lock()
	m.stopped = true
	cancel, stop := m.cancel, m.connStop
	m.mu.Unlock()
	if cancel != nil {
		cancel()
		if stop != nil {
			stop()
		}
		<-m.done
	}
}

// update takes a changed address or channel, reporting whether anything
// changed. Called with l.mu held.
func (m *member) update(w Member) bool {
	moved := m.cfg.Host != w.Host || m.cfg.Port != w.Port
	changed := moved || m.cfg != w
	m.cfg = w
	if moved {
		m.kick()
	}
	return changed
}

// kick ends the current connection so the loop redials.
func (m *member) kick() {
	m.mu.Lock()
	stop := m.connStop
	m.mu.Unlock()
	if stop != nil {
		stop()
	}
	select {
	case m.wake <- struct{}{}:
	default:
	}
}

func (m *member) keyArrived() {
	select {
	case m.wake <- struct{}{}:
	default:
	}
}

func (m *member) config() Member {
	m.l.mu.Lock()
	defer m.l.mu.Unlock()
	return m.cfg
}

func (m *member) setState(state, detail string) {
	m.mu.Lock()
	changed := m.state != state || m.detail != detail
	m.state, m.detail = state, detail
	m.mu.Unlock()
	if changed {
		m.l.changed()
	}
}

func (m *member) status() MemberStatus {
	cfg := m.config()
	m.mu.Lock()
	defer m.mu.Unlock()
	return MemberStatus{ID: cfg.ID, ClientID: cfg.ClientID, Channel: cfg.Channel, State: m.state, Detail: m.detail}
}

// ── the send queue ─────────────────────────────────────────────────────────
//
// Producers are the pump and the leader's control calls, all under l.mu, and
// none may block: a slow player must cost the others nothing. A queue that
// grows past leaderQueueChunks drops the connection instead, so the player
// resyncs from the live stream rather than falling further behind.

func (m *member) push(msg outMsg) {
	m.qmu.Lock()
	if len(m.items) >= leaderQueueChunks && msg.kind == outAudio {
		m.overflow = true
		m.qmu.Unlock()
		m.kick()
		return
	}
	m.items = append(m.items, msg)
	m.qmu.Unlock()
	select {
	case m.notify <- struct{}{}:
	default:
	}
}

func (m *member) enqueueAudio(ts int64, frame []byte) {
	m.push(outMsg{kind: outAudio, ts: ts, data: frame})
}
func (m *member) enqueueStart() { m.push(outMsg{kind: outStart}) }

func (m *member) sendVolumeLocked(pct int) {
	if !m.ready || m.sentVol == pct {
		return
	}
	m.sentVol = pct
	m.push(outMsg{kind: outVolume, vol: pct})
}

// clearQueue drops audio not yet sent and tells the player to drop what it
// holds. Start and volume messages stay: a clear must not leave a player
// that is about to be sent audio without ever having been told the format.
func (m *member) clearQueue() {
	m.dropAudio()
	if m.ready {
		m.push(outMsg{kind: outClear})
	}
}

func (m *member) clearQueueQuiet() {
	m.dropAudio()
	m.sentVol = -1
}

func (m *member) dropAudio() {
	m.qmu.Lock()
	keep := m.items[:0]
	for _, it := range m.items {
		if it.kind != outAudio {
			keep = append(keep, it)
		}
	}
	m.items = keep
	m.qmu.Unlock()
}

func (m *member) pop() (outMsg, bool) {
	m.qmu.Lock()
	defer m.qmu.Unlock()
	if len(m.items) == 0 {
		return outMsg{}, false
	}
	it := m.items[0]
	m.items[0] = outMsg{}
	m.items = m.items[1:]
	return it, true
}

func (m *member) resetQueue() {
	m.qmu.Lock()
	m.items, m.overflow = nil, false
	m.qmu.Unlock()
}

// ── the connection loop ────────────────────────────────────────────────────

type leaderEnd struct {
	state  string
	detail string
	wait   time.Duration
	auth   bool // the peer refused our credentials
	served bool // reached ready at least once
}

func (m *member) loop(ctx context.Context) {
	backoff := backoffMin
	authFails := 0
	for ctx.Err() == nil {
		cfg := m.config()
		psk := m.l.store.key(cfg.ClientID)
		if psk == nil {
			m.setState(StateNeedsKey, "no key for this Echo yet")
			if !m.wait(ctx, 0) {
				return
			}
			continue
		}
		m.setState(StateConnecting, "")
		end := m.serve(ctx, cfg, psk)
		if ctx.Err() != nil {
			return
		}
		if end.served {
			backoff, authFails = backoffMin, 0
		}
		if end.auth {
			authFails++
			if authFails >= authFailsMax {
				// The player does not know our key (reset, or it was
				// re-provisioned). Forget it; the controller mints a new one.
				m.l.store.dropKey(cfg.ClientID)
				authFails = 0
				m.setState(StateNeedsKey, "the key was refused")
				continue
			}
		} else {
			authFails = 0
		}
		m.setState(end.state, end.detail)
		wait := backoff
		if end.wait > 0 {
			wait = end.wait
		}
		if !m.wait(ctx, wait) {
			return
		}
		backoff = min(backoff*2, backoffMax)
	}
}

// wait sleeps d (or, with 0, until something changes), reporting false when
// the member is being shut down.
func (m *member) wait(ctx context.Context, d time.Duration) bool {
	if d == 0 {
		select {
		case <-ctx.Done():
			return false
		case <-m.wake:
			return true
		}
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-m.wake:
		return true
	case <-t.C:
		return true
	}
}

// leaderConn is the encrypted transport of one connection.
type leaderConn struct {
	ws     *websocket.Conn
	sendMu sync.Mutex
	tr     *transport
	closed bool
	ra     reassembler
}

func (c *leaderConn) sendJSON(typ string, payload any) error {
	b, err := marshalMsg(typ, payload)
	if err != nil {
		return err
	}
	return c.sendRaw(append([]byte{msgJSON}, b...))
}

func (c *leaderConn) sendAudio(ts int64, frame []byte) error {
	b := make([]byte, 9+len(frame))
	b[0] = msgAudioChunk
	binary.BigEndian.PutUint64(b[1:9], uint64(ts))
	copy(b[9:], frame)
	return c.sendRaw(b)
}

func (c *leaderConn) sendRaw(pt []byte) error {
	c.sendMu.Lock()
	defer c.sendMu.Unlock()
	if c.closed {
		return errors.New("sendspin: connection closed")
	}
	ct, err := c.tr.encrypt(pt)
	if err != nil {
		return err
	}
	c.ws.SetWriteDeadline(time.Now().Add(leaderWriteWait))
	return c.ws.WriteMessage(websocket.BinaryMessage, ct)
}

func (c *leaderConn) readMsg() ([]byte, error) {
	for {
		mt, data, err := c.ws.ReadMessage()
		if err != nil {
			return nil, err
		}
		if mt != websocket.BinaryMessage {
			return nil, fmt.Errorf("%w: cleartext frame after handshake", errProtocol)
		}
		pt, err := c.tr.decrypt(data)
		if err != nil {
			return nil, fmt.Errorf("%w: transport message failed authentication", errProtocol)
		}
		msg, err := c.ra.push(pt)
		if err != nil {
			return nil, err
		}
		if msg != nil {
			return msg, nil
		}
	}
}

func (c *leaderConn) close() {
	c.sendMu.Lock()
	c.closed = true
	c.sendMu.Unlock()
	c.ws.Close()
}

// readJSON returns the next JSON message, ignoring anything else.
func (c *leaderConn) readJSON() (envelope, error) {
	for {
		msg, err := c.readMsg()
		if err != nil {
			return envelope{}, err
		}
		if msg[0] != msgJSON {
			continue
		}
		var env envelope
		if err := json.Unmarshal(msg[1:], &env); err != nil {
			return envelope{}, fmt.Errorf("%w: malformed JSON", errProtocol)
		}
		return env, nil
	}
}

// serve runs one connection to its end.
func (m *member) serve(ctx context.Context, cfg Member, psk []byte) (end leaderEnd) {
	cctx, cancel := context.WithCancel(ctx)
	defer cancel()
	m.mu.Lock()
	if m.stopped {
		m.mu.Unlock()
		return leaderEnd{state: StateConnecting}
	}
	m.connStop = cancel
	m.servedOnce = false
	m.mu.Unlock()
	defer func() {
		m.mu.Lock()
		m.connStop = nil
		m.mu.Unlock()
	}()

	url := "ws://" + net.JoinHostPort(cfg.Host, strconv.Itoa(cfg.Port)) + Path
	d := websocket.Dialer{HandshakeTimeout: dialTimeout}
	ws, _, err := d.DialContext(cctx, url, nil)
	if err != nil {
		return leaderEnd{state: StateUnreachable, detail: "cannot reach this Echo"}
	}
	c := &leaderConn{ws: ws}
	ws.SetReadLimit(maxReadBytes)
	go func() { <-cctx.Done(); c.close() }()
	defer c.close()

	c.tr, err = m.handshake(c, cfg, psk)
	if err != nil {
		var he *handshakeError
		if errors.As(err, &he) {
			return leaderEnd{state: StateError, detail: he.msg, auth: he.auth}
		}
		return leaderEnd{state: StateUnreachable, detail: "connection lost"}
	}

	// Only the transport is encrypted from here.
	if err := c.sendJSON("server/hello", serverHello{Name: m.l.groupName()}); err != nil {
		return leaderEnd{state: StateUnreachable, detail: "connection lost"}
	}
	ws.SetReadDeadline(time.Now().Add(handshakeTimeout))
	env, err := c.readJSON()
	var ch clientHello
	if err != nil || env.Type != "client/hello" || json.Unmarshal(env.Payload, &ch) != nil {
		return leaderEnd{state: StateError, detail: "this Echo did not answer the hello"}
	}
	if detail := playerProblem(ch); detail != "" {
		return leaderEnd{state: StateError, detail: detail}
	}
	roles := []string{rolePlayer}
	if err := c.sendJSON("server/activate", serverActivate{Activities: []string{actPlayback}, ActiveRoles: &roles}); err != nil {
		return leaderEnd{state: StateUnreachable, detail: "connection lost"}
	}

	// Reader: time requests are answered here, at once; the player's state
	// decides whether it is free to be sent audio.
	readDone := make(chan leaderEnd, 1)
	go func() { readDone <- m.read(c, cfg) }()

	// Writer.
	defer func() {
		m.l.deactivate(m)
		m.resetQueue()
		end.served = end.served || m.wasReady()
	}()
	for {
		for {
			it, ok := m.pop()
			if !ok {
				break
			}
			if err := m.write(c, it); err != nil {
				cancel()
				return <-readDone
			}
		}
		m.qmu.Lock()
		over := m.overflow
		m.qmu.Unlock()
		if over {
			log.Printf("[group] %s fell too far behind; resyncing", cfg.ID)
			cancel()
			e := <-readDone
			e.state, e.detail = StateConnecting, "fell behind; resyncing"
			e.wait = 500 * time.Millisecond
			return e
		}
		select {
		case <-m.notify:
		case e := <-readDone:
			return e
		case <-cctx.Done():
			return <-readDone
		}
	}
}

func (m *member) wasReady() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.servedOnce
}

func (m *member) write(c *leaderConn, it outMsg) error {
	switch it.kind {
	case outAudio:
		return c.sendAudio(it.ts, it.data)
	case outStart:
		enc, err := newFlacEncoder()
		if err != nil {
			return err
		}
		if err := c.sendJSON("group/update", groupUpdate{
			PlaybackState: "playing", GroupID: m.l.ServerID(), GroupName: m.l.groupName()}); err != nil {
			return err
		}
		return c.sendJSON("stream/start", streamStart{Player: &streamFormat{
			Codec: "flac", SampleRate: outRate, Channels: outChannels, BitDepth: outBits, Header: enc.header}})
	case outClear:
		return c.sendJSON("stream/clear", streamRoles{Roles: []string{"player"}})
	case outVolume:
		v := it.vol
		return c.sendJSON("server/command", serverCommand{Player: &playerCommand{Command: "volume", Volume: &v}})
	}
	return nil
}

// playerProblem says why a player cannot be led, or "".
func playerProblem(ch clientHello) string {
	has := false
	for _, r := range ch.SupportedRoles {
		if r == rolePlayer {
			has = true
		}
	}
	if !has || ch.PlayerSupport == nil {
		return "this Echo is not a Sendspin player"
	}
	for _, f := range ch.PlayerSupport.SupportedFormats {
		if f.Codec == "flac" && f.SampleRate == outRate && f.BitDepth == outBits && f.Channels == outChannels {
			return ""
		}
	}
	return "this Echo does not take 48kHz mono FLAC"
}

// read serves the player's messages until the connection ends.
func (m *member) read(c *leaderConn, cfg Member) leaderEnd {
	defer c.close()
	c.ws.SetReadDeadline(time.Now().Add(leaderIdleTimeout))
	for {
		msg, err := c.readMsg()
		received := nowUs()
		if err != nil {
			return leaderEnd{state: StateUnreachable, detail: "connection lost"}
		}
		c.ws.SetReadDeadline(time.Now().Add(leaderIdleTimeout))
		if msg[0] != msgJSON {
			continue
		}
		var env envelope
		if json.Unmarshal(msg[1:], &env) != nil {
			return leaderEnd{state: StateError, detail: "malformed message"}
		}
		switch env.Type {
		case "client/time":
			var t clientTime
			if json.Unmarshal(env.Payload, &t) != nil {
				continue
			}
			if err := c.sendJSON("server/time", serverTime{
				ClientTransmitted: t.ClientTransmitted, ServerReceived: received, ServerTransmitted: nowUs()}); err != nil {
				return leaderEnd{state: StateUnreachable, detail: "connection lost"}
			}
		case "client/state":
			var st clientState
			if json.Unmarshal(env.Payload, &st) != nil {
				continue
			}
			m.onState(st)
		case "client/goodbye":
			var g clientGoodbye
			json.Unmarshal(env.Payload, &g)
			return goodbyeEnd(g.Reason)
		}
	}
}

func goodbyeEnd(reason string) leaderEnd {
	switch reason {
	case "restart", "user_request", "shutdown":
		return leaderEnd{state: StateUnreachable, detail: "this Echo is restarting its player"}
	case "another_server", "concurrent_attempt":
		return leaderEnd{state: StateBusy, detail: "another server is using this Echo", wait: busyBackoff}
	}
	return leaderEnd{state: StateError, detail: "this Echo refused: " + reason, wait: busyBackoff}
}

// onState handles client/state: available means free to play. The first one
// only comes once the player's clock has converged, so a player that is
// available is also in sync.
func (m *member) onState(st clientState) {
	if st.Available {
		m.mu.Lock()
		m.servedOnce = true
		m.mu.Unlock()
		m.l.mu.Lock()
		was := m.ready
		m.l.mu.Unlock()
		if !was {
			m.setState(StateReady, "")
			m.l.activate(m)
		}
		return
	}
	m.l.deactivate(m)
	m.setState(StateBusy, "this Echo is playing something else")
}

// ── the handshake ──────────────────────────────────────────────────────────

type handshakeError struct {
	msg  string
	auth bool
}

func (e *handshakeError) Error() string { return e.msg }

func (m *member) handshake(c *leaderConn, cfg Member, psk []byte) (*transport, error) {
	c.ws.SetReadDeadline(time.Now().Add(handshakeTimeout))
	mt, clientInitBytes, err := c.ws.ReadMessage()
	if err != nil || mt != websocket.TextMessage {
		return nil, errors.New("no client/init")
	}
	var env envelope
	var ci clientInit
	if json.Unmarshal(clientInitBytes, &env) != nil || env.Type != "client/init" ||
		json.Unmarshal(env.Payload, &ci) != nil || ci.Version != 1 || ci.Suite != suiteName {
		return nil, &handshakeError{msg: "this Echo speaks a different Sendspin"}
	}
	if ci.ClientID != cfg.ClientID {
		return nil, &handshakeError{msg: "this address is a different Echo than expected"}
	}
	peer, err := peerKey(cfg.ClientID)
	if err != nil {
		return nil, &handshakeError{msg: "bad client id"}
	}
	id := m.l.store.id
	initOut, err := marshalMsg("server/init", serverInit{ServerID: id.clientID(), Version: 1})
	if err != nil {
		return nil, err
	}
	c.ws.SetWriteDeadline(time.Now().Add(handshakeTimeout))
	if err := c.ws.WriteMessage(websocket.TextMessage, initOut); err != nil {
		return nil, err
	}

	prologue := append(append([]byte(nil), clientInitBytes...), initOut...)
	hs, err := noise.NewHandshakeState(noise.Config{
		CipherSuite:           cipherSuite,
		Pattern:               noise.HandshakeKK,
		Initiator:             true,
		Prologue:              prologue,
		StaticKeypair:         noise.DHKey{Private: id.priv, Public: id.pub},
		PeerStatic:            peer,
		PresharedKey:          psk,
		PresharedKeyPlacement: 2,
	})
	if err != nil {
		return nil, err
	}
	payload, _ := json.Marshal(msg1Payload{PskID: pskID(psk), PskCategory: "lt"})
	msg1, _, _, err := hs.WriteMessage(nil, payload)
	if err != nil {
		return nil, err
	}
	out, _ := marshalMsg("noise/handshake", noiseHandshake{Data: b64u(msg1)})
	if err := c.ws.WriteMessage(websocket.TextMessage, out); err != nil {
		return nil, err
	}

	c.ws.SetReadDeadline(time.Now().Add(handshakeTimeout))
	mt, data, err := c.ws.ReadMessage()
	if err != nil || mt != websocket.TextMessage {
		// The player closes the socket when it cannot place message 1.
		return nil, &handshakeError{msg: "this Echo did not accept the key", auth: true}
	}
	var menv envelope
	var nh noiseHandshake
	if json.Unmarshal(data, &menv) != nil || menv.Type != "noise/handshake" || json.Unmarshal(menv.Payload, &nh) != nil {
		return nil, &handshakeError{msg: "unexpected reply to the handshake"}
	}
	msg2, err := unb64u(nh.Data)
	if err != nil {
		return nil, &handshakeError{msg: "unexpected reply to the handshake"}
	}
	_, cs1, cs2, err := hs.ReadMessage(nil, msg2)
	if err != nil || cs1 == nil || cs2 == nil {
		return nil, &handshakeError{msg: "this Echo did not accept the key", auth: true}
	}
	// cs1 carries initiator to responder, cs2 the way back.
	return &transport{send: cs1, recv: cs2}, nil
}

func (l *Leader) groupName() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.name
}
