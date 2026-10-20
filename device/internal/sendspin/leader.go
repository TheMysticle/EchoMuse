package sendspin

import (
	"errors"
	"log"
	"sort"
	"sync"
	"sync/atomic"
	"time"
)

// A Leader is a Sendspin SERVER on an Echo: it takes the Echo's own music
// (Spotify, from the ring the engine writes) and plays it, in step, on this
// Echo and on the Echoes it leads.
//
// The players it leads are the ordinary Sendspin player (client.go) with
// nothing added to the wire: the Leader dials them, the way Music Assistant
// does, and speaks the same Noise-encrypted protocol with the same
// timestamps. What changes is only where the timestamps come from. Spotify
// audio is not placed in time (internal/spotify plays it as it arrives), so
// the Leader places it: each 20ms period is stamped on this Echo's clock a
// little in the future, sent, and played HERE through the very scheduler the
// players use (player.go) against an identity clock, so the leader is
// not early or late against its own followers.
//
// It exists only on an Echo that was told to lead. An Echo that was not has
// no Leader, no goroutine and no listener: the cost of the feature to
// everything else is the pointer test in the music mux.
const (
	// leaderChunkFrames is one chunk: 20ms, a whole FLAC block.
	leaderChunkFrames = 960
	leaderChunkBytes  = leaderChunkFrames * 4 // stereo S16LE
	leaderChunkDur    = 20 * time.Millisecond
	leaderChunkUs     = int64(leaderChunkDur / time.Microsecond)

	// LeaderStartLead is how far in the future the first chunk of a run is
	// placed: the delay between the audio being ready and it being heard,
	// and the only latency a group adds to starting a song.
	LeaderStartLead = 500 * time.Millisecond
	// LeaderAhead is how much audio is kept sent-but-not-yet-played once the
	// run is going. It is built in the first second by sending faster than
	// real time (the decoder is faster than real time), so it costs no
	// start latency; it is the margin a stalled WiFi link has to recover in.
	// It is also what a skip made on the phone has to play out, since the
	// engine reports it only when it happens, not when it is heard.
	LeaderAhead = 2 * time.Second

	// leaderQueueChunks is how much one player may fall behind before it is
	// dropped and resynced rather than allowed to hold memory: 8s.
	leaderQueueChunks = 400
	// leaderHistChunks bounds what is kept for a pause: LeaderAhead plus
	// slack.
	leaderHistChunks = int((LeaderAhead+time.Second)/leaderChunkDur) + 1

	// pauseMargin is how far past the moment of a pause the kept audio starts:
	// what the players had already handed to their DAC (their hardware buffer
	// is ~85ms) still plays after the clear, and the kept audio must follow
	// it rather than repeat it.
	pauseMargin = 100 * time.Millisecond

	leaderIdleWait   = 100 * time.Millisecond
	leaderSrcWait    = leaderChunkDur
	leaderIdleSrcGap = 250 * time.Millisecond
)

// Channel is which part of the stereo signal a player gets.
type Channel string

const (
	ChannelMono  Channel = "mono"
	ChannelLeft  Channel = "left"
	ChannelRight Channel = "right"
)

// ParseChannel reads a channel name; anything else is mono, which is what an
// Echo plays when nobody said otherwise.
func ParseChannel(s string) Channel {
	switch Channel(s) {
	case ChannelLeft, ChannelRight:
		return Channel(s)
	}
	return ChannelMono
}

// Member is one Echo the leader leads.
type Member struct {
	ID       string // the controller's device id, echoed back in status
	ClientID string // its Sendspin identity (public key), pinned
	Host     string
	Port     int
	Channel  Channel
}

// AudioSource is what the leader reads: 48kHz stereo S16LE, already paced by
// back-pressure the way the Spotify ring is.
type AudioSource interface {
	// Read fills out with exactly len(out) bytes, waiting up to wait for them.
	Read(out []byte, wait time.Duration) bool
	// Idle is true when nothing is queued and the engine is not playing.
	Idle() bool
	// Events reports, and forgets, that audio was thrown away (a skip, a
	// stop, another source taking over) or paused since the last call.
	Events() (discarded, paused bool)
	// Paused is whether the engine is paused right now, as opposed to the
	// edge Events reports once. Audio that still arrives while it is true is
	// the tail of the pause, not playback starting again.
	Paused() bool
}

// LeaderConfig is what the device supplies.
type LeaderConfig struct {
	StorePath string
	Name      string
	Version   string
}

// Leader plays and distributes. All exported methods are safe for concurrent
// use; Fill and Active are the speaker's and are called from one goroutine.
type Leader struct {
	cfg   LeaderConfig
	store *leaderStore

	local *player
	oc    atomic.Pointer[OutputClock]

	engaged atomic.Bool

	mu       sync.Mutex
	name     string
	own      Channel
	members  map[string]*member // by client id
	hist     []histChunk
	replay   []histChunk
	free     [][]byte
	encs     map[Channel]*flacEncoder
	volume   int // percent; -1 until the device says
	onChange func()

	src    AudioSource
	stop   chan struct{}
	done   chan struct{}
	closed bool
}

type histChunk struct {
	ts int64
	b  []byte // stereo S16LE, leaderChunkBytes
}

// NewLeader loads (or creates) the leader's identity. It does not run until
// Start.
func NewLeader(cfg LeaderConfig) (*Leader, error) {
	st, err := openLeaderStore(cfg.StorePath)
	if err != nil {
		return nil, err
	}
	l := &Leader{
		cfg:     cfg,
		store:   st,
		local:   newPlayer(newIdentityFilter()),
		name:    cfg.Name,
		own:     ChannelMono,
		members: map[string]*member{},
		encs:    map[Channel]*flacEncoder{},
		volume:  -1,
	}
	return l, nil
}

// newIdentityFilter is the leader's clock against itself: server time is
// local time, exactly, from the first sample.
func newIdentityFilter() *timeFilter { return &timeFilter{count: 2, offsetCov: 1} }

// ServerID is the leader's identity. The controller hands it to each player
// together with the key.
func (l *Leader) ServerID() string { return l.store.serverID() }

// OnChange is called (on its own goroutine) whenever the status moves.
func (l *Leader) OnChange(f func()) {
	l.mu.Lock()
	l.onChange = f
	l.mu.Unlock()
}

func (l *Leader) changed() {
	l.mu.Lock()
	f := l.onChange
	l.mu.Unlock()
	if f != nil {
		go f()
	}
}

// Start begins reading src. Calling it again with the leader running is a
// programming error.
func (l *Leader) Start(src AudioSource) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.closed {
		return errors.New("sendspin: leader closed")
	}
	if l.src != nil {
		return errors.New("sendspin: leader already started")
	}
	l.src = src
	l.stop, l.done = make(chan struct{}), make(chan struct{})
	go l.run(src, l.stop, l.done)
	return nil
}

// Close ends every connection and the pump, and empties the local queue.
func (l *Leader) Close() {
	l.mu.Lock()
	if l.closed {
		l.mu.Unlock()
		return
	}
	l.closed = true
	stop, done := l.stop, l.done
	var ms []*member
	for _, m := range l.members {
		ms = append(ms, m)
	}
	l.members = map[string]*member{}
	l.mu.Unlock()
	for _, m := range ms {
		m.shutdown()
	}
	if stop != nil {
		close(stop)
		<-done
	}
	l.engaged.Store(false)
	l.local.clear()
}

// SetMembers makes the set of led players match ms, and sets this Echo's own
// channel and the group's name. Players that stay keep their connection;
// one whose address changed is redialled; one that left is said goodbye to.
func (l *Leader) SetMembers(name string, own Channel, ms []Member) {
	want := map[string]Member{}
	for _, m := range ms {
		if m.ClientID == "" || m.Host == "" {
			continue
		}
		if m.Port == 0 {
			m.Port = DefaultPort
		}
		m.Channel = ParseChannel(string(m.Channel))
		want[m.ClientID] = m
	}
	l.mu.Lock()
	if l.closed {
		l.mu.Unlock()
		return
	}
	moved := false
	if name != "" && name != l.name {
		l.name, moved = name, true
	}
	if o := ParseChannel(string(own)); o != l.own {
		l.own, moved = o, true
	}
	var gone []*member
	for id, m := range l.members {
		if _, ok := want[id]; !ok {
			gone = append(gone, m)
			delete(l.members, id)
		}
	}
	var fresh []*member
	for id, w := range want {
		if m, ok := l.members[id]; ok {
			if m.update(w) {
				moved = true
			}
			continue
		}
		m := newMember(l, w)
		l.members[id] = m
		fresh = append(fresh, m)
	}
	l.mu.Unlock()
	for _, m := range gone {
		m.shutdown()
	}
	for _, m := range fresh {
		m.start()
	}
	if moved || len(gone) > 0 || len(fresh) > 0 {
		l.changed()
	}
}

// SetKey installs the long-term PSK the controller minted for one player.
func (l *Leader) SetKey(clientID string, psk []byte) error {
	if err := l.store.setKey(clientID, psk); err != nil {
		return err
	}
	l.mu.Lock()
	m := l.members[clientID]
	l.mu.Unlock()
	if m != nil {
		m.keyArrived()
	}
	return nil
}

// SetVolume is the leader's volume, 0-100, passed on to the players. Only a
// change is sent.
func (l *Leader) SetVolume(pct int) {
	pct = clamp(pct, 0, 100)
	l.mu.Lock()
	if l.volume == pct {
		l.mu.Unlock()
		return
	}
	l.volume = pct
	for _, m := range l.members {
		m.sendVolumeLocked(pct)
	}
	l.mu.Unlock()
}

func (l *Leader) volumeNow() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.volume
}

// readyCount is how many players are connected, in sync and free to play.
func (l *Leader) readyCount() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	n := 0
	for _, m := range l.members {
		if m.ready {
			n++
		}
	}
	return n
}

// ── the speaker's side ─────────────────────────────────────────────────────

// Engaged says whether the leader is driving this Echo's music. While it is,
// the speaker pulls from Fill and the engine's ring belongs to the pump.
func (l *Leader) Engaged() bool { return l.engaged.Load() }

// Active reports whether audio is queued to play here.
func (l *Leader) Active() bool { return l.engaged.Load() && l.local.active() }

// Fill is the speaker's pull, like Client.Fill: one stereo period whose first
// frame reaches the DAC at playAt as measured. Called from one goroutine.
func (l *Leader) Fill(out []byte, playAt time.Time, uncertain time.Duration) bool {
	oc := l.oc.Load()
	if oc == nil {
		oc = NewOutputClock(time.Duration(len(out)/4) * time.Second / outRate)
		l.oc.Store(oc)
	}
	var at time.Time
	if uncertain > outClockTrust {
		at = oc.Coast(playAt)
	} else {
		at = oc.Observe(playAt)
	}
	return l.local.Fill(out, at)
}

// Buffered is how much audio is queued here and not yet played.
func (l *Leader) Buffered() time.Duration { return l.local.buffered() }

// SetGain applies the leader's own mute (not the volume: the device applies
// that to everything it plays).
func (l *Leader) SetGain(volume int, muted bool) { l.local.setGain(volume, muted) }

// OutDiag takes the DAC position tracker's summary since the last call.
func (l *Leader) OutDiag() OutDiag {
	if oc := l.oc.Load(); oc != nil {
		return oc.TakeDiag()
	}
	return OutDiag{}
}

// ── the pump ───────────────────────────────────────────────────────────────

func (l *Leader) run(src AudioSource, stop, done chan struct{}) {
	defer close(done)
	buf := make([]byte, leaderChunkBytes)
	var (
		anchored bool
		nextTs   int64
	)
	ahead := LeaderAhead.Microseconds()
	for {
		select {
		case <-stop:
			return
		default:
		}

		if disc, paused := src.Events(); disc || paused {
			l.resetTimeline(src, paused && !disc)
			anchored = false
		}

		if !l.engaged.Load() {
			// Taken at rest, never mid-song: a switch while audio is moving
			// would splice two timelines.
			if src.Idle() && l.readyCount() > 0 {
				l.engaged.Store(true)
				l.changed()
			} else {
				if !sleepOrStop(stop, leaderIdleWait) {
					return
				}
				continue
			}
		}
		if src.Idle() && !anchored && l.readyCount() == 0 && !l.local.active() {
			l.engaged.Store(false)
			l.changed()
			continue
		}

		now := nowUs()
		if anchored && nextTs-now >= ahead {
			d := time.Duration(nextTs-ahead-now) * time.Microsecond
			if !sleepOrStop(stop, max(time.Millisecond, min(d, leaderChunkDur))) {
				return
			}
			continue
		}
		wait := leaderSrcWait
		if !anchored && src.Idle() {
			wait = leaderIdleSrcGap
		}
		if !src.Read(buf, wait) {
			if anchored && nextTs < nowUs() {
				anchored = false // caught up with the clock: the next audio gets a fresh lead
			}
			continue
		}
		if !anchored && src.Paused() {
			// The tail of a pause (the engine's sink flushing what its
			// resampler held), not playback starting. Starting here would
			// replay everything the pause kept, which is heard as the
			// music cutting out and then coming back for two seconds.
			// It belongs after the kept audio, for the resume.
			l.keepForResume(buf)
			continue
		}
		if !anchored {
			nextTs, anchored = nowUs()+LeaderStartLead.Microseconds(), true
			nextTs = l.emitReplay(nextTs)
		}
		l.emit(nextTs, buf)
		nextTs += leaderChunkUs
	}
}

func sleepOrStop(stop <-chan struct{}, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-stop:
		return false
	case <-t.C:
		return true
	}
}

// emit sends one stereo chunk to the local queue and to every ready player,
// and remembers it for a pause.
func (l *Leader) emit(ts int64, stereo []byte) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.emitLocked(ts, stereo)
}

func (l *Leader) emitLocked(ts int64, stereo []byte) {
	l.local.push(ts, splitChannel(stereo, l.own))
	var frames map[Channel][]byte
	for _, m := range l.members {
		if !m.ready {
			continue
		}
		if frames == nil {
			frames = map[Channel][]byte{}
		}
		f, ok := frames[m.cfg.Channel]
		if !ok {
			f = l.encodeLocked(m.cfg.Channel, stereo)
			frames[m.cfg.Channel] = f
		}
		if f != nil {
			m.enqueueAudio(ts, f)
		}
	}
	h := histChunk{ts: ts, b: l.takeBuf()}
	copy(h.b, stereo)
	l.hist = append(l.hist, h)
	// What has been played is not needed for a pause or a late joiner.
	now := nowUs()
	drop := 0
	for drop < len(l.hist)-1 && (l.hist[drop].ts < now-200_000 || len(l.hist)-drop > leaderHistChunks) {
		l.free = append(l.free, l.hist[drop].b)
		l.hist[drop].b = nil
		drop++
	}
	if drop > 0 {
		l.hist = append(l.hist[:0], l.hist[drop:]...)
	}
}

func (l *Leader) takeBuf() []byte {
	if n := len(l.free); n > 0 {
		b := l.free[n-1]
		l.free = l.free[:n-1]
		return b
	}
	return make([]byte, leaderChunkBytes)
}

// encodeLocked returns the FLAC frame for one channel variant, or nil if it
// cannot be made (logged once per failure by the caller's drop).
func (l *Leader) encodeLocked(ch Channel, stereo []byte) []byte {
	e := l.encs[ch]
	if e == nil {
		var err error
		if e, err = newFlacEncoder(); err != nil {
			log.Printf("[group] flac encoder: %v", err)
			return nil
		}
		l.encs[ch] = e
	}
	frame, err := e.encode(splitChannel(stereo, ch))
	if err != nil {
		log.Printf("[group] flac encode: %v", err)
		return nil
	}
	return append([]byte(nil), frame...)
}

// splitChannel turns a stereo chunk into the mono samples a player plays.
func splitChannel(stereo []byte, ch Channel) []int16 {
	n := len(stereo) / 4
	out := make([]int16, n)
	for i := 0; i < n; i++ {
		l := int16(uint16(stereo[i*4]) | uint16(stereo[i*4+1])<<8)
		r := int16(uint16(stereo[i*4+2]) | uint16(stereo[i*4+3])<<8)
		switch ch {
		case ChannelLeft:
			out[i] = l
		case ChannelRight:
			out[i] = r
		default:
			out[i] = int16((int32(l) + int32(r)) >> 1)
		}
	}
	return out
}

// resetTimeline throws away what is in flight everywhere: a skip, a stop, or
// a pause. After a pause the audio that had been sent but not yet heard is
// kept and goes out again first when playback resumes, so a pause does not
// swallow the seconds that were buffered ahead of it.
func (l *Leader) resetTimeline(src AudioSource, keep bool) {
	// The engine's ring holds a few more periods decoded before the pause.
	var tail [][]byte
	if keep {
		for {
			b := make([]byte, leaderChunkBytes)
			if !src.Read(b, 0) {
				break
			}
			tail = append(tail, b)
		}
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	now := nowUs()
	// What a stop cuts off is the measure of how far ahead the players were;
	// logged so a stop that seems late can be told from one that was not.
	var unplayed int64
	for _, h := range l.hist {
		if h.ts > now {
			unplayed += leaderChunkUs
		}
	}
	log.Printf("[group] stop: %dms of audio had been sent ahead (keep=%v)", unplayed/1000, keep)
	l.local.clear()
	for _, m := range l.members {
		m.clearQueue()
	}
	if !keep {
		l.replay = l.replay[:0]
	} else {
		// Appended, not replaced: a second pause before any audio has
		// moved again finds the history empty and must not lose what the
		// first one kept.
		for _, h := range l.hist {
			if h.ts > now+pauseMargin.Microseconds() {
				l.replay = append(l.replay, histChunk{ts: h.ts, b: append([]byte(nil), h.b...)})
			}
		}
		for _, b := range tail {
			l.replay = append(l.replay, histChunk{b: b})
		}
	}
	for _, h := range l.hist {
		l.free = append(l.free, h.b)
	}
	l.hist = l.hist[:0]
}

// keepForResume adds one period to the audio a pause kept.
func (l *Leader) keepForResume(b []byte) {
	l.mu.Lock()
	l.replay = append(l.replay, histChunk{b: append([]byte(nil), b...)})
	l.mu.Unlock()
}

// emitReplay plays out what a pause kept, ahead of the live audio, and
// returns where the live audio now starts.
func (l *Leader) emitReplay(ts int64) int64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	for _, h := range l.replay {
		l.emitLocked(ts, h.b)
		ts += leaderChunkUs
	}
	l.replay = l.replay[:0]
	return ts
}

// activate starts a player that has just become free. Under the lock so no
// chunk slips between its stream/start and the catch-up: it joins mid-song
// with the audio already in flight, not two seconds late.
func (l *Leader) activate(m *member) {
	l.mu.Lock()
	m.ready = true
	m.enqueueStart()
	if l.volume >= 0 {
		m.sendVolumeLocked(l.volume)
	}
	if l.engaged.Load() {
		now := nowUs()
		for _, h := range l.hist {
			if h.ts > now+100_000 {
				if f := l.encodeLocked(m.cfg.Channel, h.b); f != nil {
					m.enqueueAudio(h.ts, f)
				}
			}
		}
	}
	l.mu.Unlock()
	l.changed()
}

func (l *Leader) deactivate(m *member) {
	l.mu.Lock()
	was := m.ready
	m.ready = false
	m.clearQueueQuiet()
	l.mu.Unlock()
	if was {
		l.changed()
	}
}

// ── status ─────────────────────────────────────────────────────────────────

// MemberStatus is one led player, as the controller sees it.
type MemberStatus struct {
	ID       string  `json:"id"`
	ClientID string  `json:"clientId"`
	Channel  Channel `json:"channel"`
	State    string  `json:"state"` // connecting | ready | needs_key | busy | unreachable | error
	Detail   string  `json:"detail,omitempty"`
}

// LeaderStatus is everything the controller is told. No secrets: the keys
// never leave the store.
type LeaderStatus struct {
	ServerID string         `json:"serverId"`
	Name     string         `json:"name,omitempty"`
	Channel  Channel        `json:"channel"`
	Engaged  bool           `json:"engaged"`
	Members  []MemberStatus `json:"members"`
}

func (l *Leader) Status() LeaderStatus {
	l.mu.Lock()
	st := LeaderStatus{ServerID: l.store.serverID(), Name: l.name, Channel: l.own, Engaged: l.engaged.Load(),
		Members: []MemberStatus{}}
	ms := make([]*member, 0, len(l.members))
	for _, m := range l.members {
		ms = append(ms, m)
	}
	l.mu.Unlock()
	for _, m := range ms {
		st.Members = append(st.Members, m.status())
	}
	sort.Slice(st.Members, func(i, j int) bool { return st.Members[i].ID < st.Members[j].ID })
	return st
}
