package sendspin

import (
	"encoding/binary"
	"math"
	"math/rand"
	"net"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

// The leader is tested against the real player (client.go), not against a
// reading of the protocol: that player is itself held to aiosendspin 9.1.1's
// wire, so a leader it accepts, decodes and keeps in sync is speaking the
// protocol the fleet's players speak.

func freePort(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	return ln.Addr().(*net.TCPAddr).Port
}

func startPlayer(t *testing.T, name string) (*Client, int) {
	t.Helper()
	port := freePort(t)
	c, err := New(Config{StorePath: filepath.Join(t.TempDir(), "s.json"), Name: name, Port: port, Quiet: true})
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Stop("shutdown") })
	return c, port
}

func newTestLeader(t *testing.T) *Leader {
	t.Helper()
	l, err := NewLeader(LeaderConfig{StorePath: filepath.Join(t.TempDir(), "leader.json"), Name: "Test group"})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(l.Close)
	return l
}

func randomKey(t *testing.T) []byte {
	t.Helper()
	k, err := randomPSK()
	if err != nil {
		t.Fatal(err)
	}
	return k
}

// share gives a leader and a player the key the controller would have minted.
func share(t *testing.T, l *Leader, c *Client) {
	t.Helper()
	k := randomKey(t)
	if err := c.TrustServer(l.ServerID(), k); err != nil {
		t.Fatal(err)
	}
	if err := l.SetKey(c.ClientID(), k); err != nil {
		t.Fatal(err)
	}
}

func waitFor(t *testing.T, what string, d time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func memberState(l *Leader, id string) string {
	for _, m := range l.Status().Members {
		if m.ID == id {
			return m.State
		}
	}
	return ""
}

// sigL and sigR are the two channels. Each sample value says which sample it
// is (mod 14999), so a player's output can be traced back to a position.
// Track 1 sits in a disjoint range, so a skip is visible in what is heard.
const sigPeriod = 14999

func sigL(k, track int) int16 { return int16(k%sigPeriod + 1 + track*15000) }
func sigR(k, track int) int16 { return int16((k*3)%sigPeriod + 1 + track*15000) }

type fakeSource struct {
	mu      sync.Mutex
	playing bool
	pos     int
	track   int
	disc    bool
	paused  bool
	reads   int
	// pausedState is the engine's state being "paused" (Events reports the
	// edge once; this stays), and trail is how many periods the engine still
	// hands over after a pause: its sink flushes what the resampler holds.
	pausedState bool
	trail       int
}

func (f *fakeSource) set(playing bool) { f.mu.Lock(); f.playing = playing; f.mu.Unlock() }

func (f *fakeSource) Read(out []byte, wait time.Duration) bool {
	f.mu.Lock()
	if !f.playing && f.trail > 0 {
		f.trail--
	} else if !f.playing {
		f.mu.Unlock()
		time.Sleep(min(wait, 5*time.Millisecond))
		return false
	}
	for i := 0; i < len(out)/4; i++ {
		binary.LittleEndian.PutUint16(out[i*4:], uint16(sigL(f.pos+i, f.track)))
		binary.LittleEndian.PutUint16(out[i*4+2:], uint16(sigR(f.pos+i, f.track)))
	}
	f.pos += len(out) / 4
	f.reads++
	f.mu.Unlock()
	return true
}

func (f *fakeSource) Paused() bool { f.mu.Lock(); defer f.mu.Unlock(); return f.pausedState }

func (f *fakeSource) Idle() bool { f.mu.Lock(); defer f.mu.Unlock(); return !f.playing }

func (f *fakeSource) Events() (bool, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	d, p := f.disc, f.paused
	f.disc, f.paused = false, false
	return d, p
}

func (f *fakeSource) position() int { f.mu.Lock(); defer f.mu.Unlock(); return f.pos }

// dac stands in for the speaker's write loop. Its clock is exact (period k
// reaches the DAC at t0 + k periods), and it pulls each period a little ahead
// of that, as the real loop does.
type dac struct {
	fill   func(out []byte, playAt time.Time, uncertain time.Duration) bool
	t0     time.Time
	frames int
	mu     sync.Mutex
	got    []played
	stop   chan struct{}
	done   chan struct{}
}

type played struct {
	at  time.Time // when the first frame reaches the DAC
	pcm []int16
}

func startDAC(fill func([]byte, time.Time, time.Duration) bool) *dac {
	d := &dac{fill: fill, t0: time.Now().Add(60 * time.Millisecond), frames: 1024,
		stop: make(chan struct{}), done: make(chan struct{})}
	go d.run()
	return d
}

func (d *dac) run() {
	defer close(d.done)
	per := time.Duration(d.frames) * time.Second / outRate
	out := make([]byte, d.frames*4)
	for k := 0; ; k++ {
		at := d.t0.Add(time.Duration(k) * per)
		select {
		case <-d.stop:
			return
		case <-time.After(time.Until(at.Add(-40 * time.Millisecond))):
		}
		if !d.fill(out, at, 0) {
			continue
		}
		pcm := make([]int16, d.frames)
		for i := range pcm {
			pcm[i] = int16(binary.LittleEndian.Uint16(out[i*4:]))
		}
		d.mu.Lock()
		d.got = append(d.got, played{at: at, pcm: pcm})
		d.mu.Unlock()
	}
}

func (d *dac) close() { close(d.stop); <-d.done }

// samples flattens what was played into (time, value) for the non-silent
// frames.
func (d *dac) samples() (ts []time.Time, vals []int16) {
	d.mu.Lock()
	defer d.mu.Unlock()
	for _, p := range d.got {
		for i, v := range p.pcm {
			if v != 0 {
				ts = append(ts, p.at.Add(time.Duration(i)*time.Second/outRate))
				vals = append(vals, v)
			}
		}
	}
	return
}

// breaks counts places where the sequence is not one sample after another.
func breaks(vals []int16, next func(int16) int16) int {
	n := 0
	for i := 1; i < len(vals); i++ {
		if vals[i] != next(vals[i-1]) {
			n++
		}
	}
	return n
}

func nextL(v int16) int16 {
	switch v {
	case sigPeriod:
		return 1
	case sigPeriod + 15000:
		return 15001
	}
	return v + 1
}

func TestFlacEncoderRoundTripsThroughThePlayersDecoder(t *testing.T) {
	enc, err := newFlacEncoder()
	if err != nil {
		t.Fatal(err)
	}
	dec, err := newDecoder(streamFormat{Codec: "flac", SampleRate: outRate, Channels: 1, BitDepth: 16, Header: enc.header})
	if err != nil {
		t.Fatal(err)
	}
	rng := rand.New(rand.NewSource(1))
	cases := map[string]func(i int) int16{
		"silence":    func(int) int16 { return 0 },
		"full scale": func(i int) int16 { return []int16{32767, -32768}[i%2] },
		"noise":      func(int) int16 { return int16(rng.Intn(65536) - 32768) },
		"sine":       func(i int) int16 { return int16(20000 * math.Sin(float64(i)*0.05)) },
		"ramp":       func(i int) int16 { return int16(i%sigPeriod + 1) },
	}
	for name, gen := range cases {
		for round := 0; round < 3; round++ {
			pcm := make([]int16, leaderChunkFrames)
			for i := range pcm {
				pcm[i] = gen(i + round*leaderChunkFrames)
			}
			frame, err := enc.encode(pcm)
			if err != nil {
				t.Fatalf("%s: %v", name, err)
			}
			if !flacSync(frame) {
				t.Fatalf("%s: chunk does not start with a frame sync", name)
			}
			// chunkPayload must see the nine-byte header's payload start.
			msg := append([]byte{msgAudioChunk, 0, 0, 0, 0, 0, 0, 0, 7}, frame...)
			ts, payload, err := chunkPayload(msg, "flac")
			if err != nil || ts != 7 || len(payload) != len(frame) {
				t.Fatalf("%s: chunkPayload ts=%d len=%d err=%v", name, ts, len(payload), err)
			}
			got, err := dec.decode(nil, append([]byte(nil), frame...))
			if err != nil {
				t.Fatalf("%s: %v", name, err)
			}
			if len(got) != len(pcm) {
				t.Fatalf("%s: decoded %d samples, want %d", name, len(got), len(pcm))
			}
			for i := range pcm {
				if got[i] != pcm[i] {
					t.Fatalf("%s: sample %d: %d != %d", name, i, got[i], pcm[i])
				}
			}
		}
	}
}

func TestFlacEncoderRejectsAPartialBlock(t *testing.T) {
	enc, _ := newFlacEncoder()
	if _, err := enc.encode(make([]int16, 100)); err == nil {
		t.Error("a short block was encoded")
	}
}

func TestSplitChannel(t *testing.T) {
	st := make([]byte, 8)
	put := func(i int, v int16) { binary.LittleEndian.PutUint16(st[i:], uint16(v)) }
	put(0, 1000)
	put(2, -3000)
	put(4, 32767)
	put(6, 32767)
	if got := splitChannel(st, ChannelLeft); got[0] != 1000 || got[1] != 32767 {
		t.Errorf("left %v", got)
	}
	if got := splitChannel(st, ChannelRight); got[0] != -3000 || got[1] != 32767 {
		t.Errorf("right %v", got)
	}
	// The mean of two full-scale samples must not wrap.
	if got := splitChannel(st, ChannelMono); got[0] != -1000 || got[1] != 32767 {
		t.Errorf("mono %v", got)
	}
	if ParseChannel("") != ChannelMono || ParseChannel("left") != ChannelLeft || ParseChannel("x") != ChannelMono {
		t.Error("ParseChannel")
	}
}

func TestIdentityFilterIsTheLocalClock(t *testing.T) {
	f := newIdentityFilter()
	if !f.synchronized() {
		t.Fatal("not synchronized")
	}
	for _, v := range []int64{0, 1, 123_456_789, 9e12} {
		if got := f.clientTime(v); got != v {
			t.Errorf("clientTime(%d) = %d", v, got)
		}
	}
}

func TestLeaderStorePersistsIdentityAndKeys(t *testing.T) {
	path := filepath.Join(t.TempDir(), "l.json")
	a, err := openLeaderStore(path)
	if err != nil {
		t.Fatal(err)
	}
	id, _ := newIdentity()
	key := randomKey(t)
	if err := a.setKey(id.clientID(), key); err != nil {
		t.Fatal(err)
	}
	if err := a.setKey("not-an-id", key); err == nil {
		t.Error("accepted a malformed client id")
	}
	if err := a.setKey(id.clientID(), key[:5]); err == nil {
		t.Error("accepted a short key")
	}
	b, err := openLeaderStore(path)
	if err != nil {
		t.Fatal(err)
	}
	if b.serverID() != a.serverID() {
		t.Error("the server id changed across a restart")
	}
	if got := b.key(id.clientID()); string(got) != string(key) {
		t.Error("key lost across a restart")
	}
	b.dropKey(id.clientID())
	if b.key(id.clientID()) != nil {
		t.Error("dropped key still answers")
	}
}

func TestMemberWithoutAKeyAsksForOne(t *testing.T) {
	c, port := startPlayer(t, "p")
	l := newTestLeader(t)
	l.SetMembers("G", ChannelMono, []Member{{ID: "a", ClientID: c.ClientID(), Host: "127.0.0.1", Port: port}})
	waitFor(t, "needs_key", 3*time.Second, func() bool { return memberState(l, "a") == StateNeedsKey })

	share(t, l, c)
	waitFor(t, "ready after the key arrives", 8*time.Second, func() bool { return memberState(l, "a") == StateReady })
}

func TestMemberWhoseKeyWasForgottenIsMarkedNeedsKey(t *testing.T) {
	c, port := startPlayer(t, "p")
	l := newTestLeader(t)
	// The leader holds a key the player has never heard of.
	if err := l.SetKey(c.ClientID(), randomKey(t)); err != nil {
		t.Fatal(err)
	}
	l.SetMembers("G", ChannelMono, []Member{{ID: "a", ClientID: c.ClientID(), Host: "127.0.0.1", Port: port}})
	waitFor(t, "needs_key after the refusals", 12*time.Second, func() bool { return memberState(l, "a") == StateNeedsKey })
	if l.store.key(c.ClientID()) != nil {
		t.Error("a refused key was kept")
	}
}

func TestMemberAtTheWrongAddressIsNotTrusted(t *testing.T) {
	_, port := startPlayer(t, "p")
	other, _ := newIdentity()
	l := newTestLeader(t)
	k := randomKey(t)
	l.SetKey(other.clientID(), k)
	l.SetMembers("G", ChannelMono, []Member{{ID: "a", ClientID: other.clientID(), Host: "127.0.0.1", Port: port}})
	waitFor(t, "an error", 4*time.Second, func() bool { return memberState(l, "a") == StateError })
}

func TestUnreachableMemberRetries(t *testing.T) {
	l := newTestLeader(t)
	id, _ := newIdentity()
	l.SetKey(id.clientID(), randomKey(t))
	l.SetMembers("G", ChannelMono, []Member{{ID: "a", ClientID: id.clientID(), Host: "127.0.0.1", Port: freePort(t)}})
	waitFor(t, "unreachable", 4*time.Second, func() bool { return memberState(l, "a") == StateUnreachable })
}

// The whole path: a source plays, the leader stamps and sends it, the player
// decodes and schedules it, and the leader's own speaker plays it — all on
// the right channel, contiguous, and at the same moment.
func TestLeaderAndPlayerPlayTheSameSamplesAtTheSameTime(t *testing.T) {
	if testing.Short() {
		t.Skip("real time")
	}
	c, port := startPlayer(t, "p")
	l := newTestLeader(t)
	share(t, l, c)
	l.SetMembers("G", ChannelLeft, []Member{{ID: "a", ClientID: c.ClientID(), Host: "127.0.0.1", Port: port, Channel: ChannelLeft}})
	waitFor(t, "ready", 8*time.Second, func() bool { return memberState(l, "a") == StateReady })

	src := &fakeSource{}
	if err := l.Start(src); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "engaged at rest", 2*time.Second, l.Engaged)

	slave, leader := startDAC(c.Fill), startDAC(l.Fill)
	src.set(true)
	start := time.Now()
	time.Sleep(3 * time.Second)
	slave.close()
	leader.close()

	for name, d := range map[string]*dac{"player": slave, "leader": leader} {
		ts, vals := d.samples()
		if len(vals) < 48000 {
			t.Fatalf("%s: only %d samples played in 3s", name, len(vals))
		}
		if vals[0] != 1 {
			t.Errorf("%s: began at sample value %d, not the start of the audio", name, vals[0])
		}
		if n := breaks(vals, nextL); n > 3 {
			t.Errorf("%s: %d discontinuities in %d samples", name, n, len(vals))
		}
		// Starting a song costs the group's lead and little else.
		if lag := ts[0].Sub(start); lag < LeaderStartLead-100*time.Millisecond || lag > LeaderStartLead+400*time.Millisecond {
			t.Errorf("%s: first sample heard %v after play, want about %v", name, lag, LeaderStartLead)
		}
	}

	// Same sample, same instant.
	pt, pv := slave.samples()
	lt, lv := leader.samples()
	at := func(ts []time.Time, vals []int16, v int16) (time.Time, bool) {
		for i, x := range vals {
			if x == v {
				return ts[i], true
			}
		}
		return time.Time{}, false
	}
	for _, probe := range []int16{1, 5000, 12000} {
		a, ok1 := at(pt, pv, probe)
		b, ok2 := at(lt, lv, probe)
		if !ok1 || !ok2 {
			t.Fatalf("sample %d missing (player %v leader %v)", probe, ok1, ok2)
		}
		if d := a.Sub(b); d > time.Millisecond || d < -time.Millisecond {
			t.Errorf("sample %d: player and leader differ by %v", probe, d)
		}
	}
}

func TestEachPlayerGetsItsOwnChannel(t *testing.T) {
	if testing.Short() {
		t.Skip("real time")
	}
	left, lp := startPlayer(t, "left")
	right, rp := startPlayer(t, "right")
	l := newTestLeader(t)
	share(t, l, left)
	share(t, l, right)
	l.SetMembers("G", ChannelMono, []Member{
		{ID: "l", ClientID: left.ClientID(), Host: "127.0.0.1", Port: lp, Channel: ChannelLeft},
		{ID: "r", ClientID: right.ClientID(), Host: "127.0.0.1", Port: rp, Channel: ChannelRight},
	})
	waitFor(t, "both ready", 8*time.Second, func() bool {
		return memberState(l, "l") == StateReady && memberState(l, "r") == StateReady
	})
	src := &fakeSource{}
	l.Start(src)
	waitFor(t, "engaged", 2*time.Second, l.Engaged)
	dl, dr := startDAC(left.Fill), startDAC(right.Fill)
	src.set(true)
	time.Sleep(2500 * time.Millisecond)
	dl.close()
	dr.close()

	_, lv := dl.samples()
	_, rv := dr.samples()
	if len(lv) < 24000 || len(rv) < 24000 {
		t.Fatalf("too little played: left %d right %d", len(lv), len(rv))
	}
	if n := breaks(lv, nextL); n > 3 {
		t.Errorf("left channel player is not playing the left channel (%d breaks)", n)
	}
	nextR := func(v int16) int16 { return int16((int(v)-1+3)%sigPeriod + 1) }
	// R = (3k mod 14999)+1 steps by 3 modulo 14999.
	if n := breaks(rv, nextR); n > 3 {
		t.Errorf("right channel player is not playing the right channel (%d breaks)", n)
	}
}

func TestPauseKeepsWhatWasBufferedAndSkipDropsIt(t *testing.T) {
	if testing.Short() {
		t.Skip("real time")
	}
	c, port := startPlayer(t, "p")
	l := newTestLeader(t)
	share(t, l, c)
	l.SetMembers("G", ChannelLeft, []Member{{ID: "a", ClientID: c.ClientID(), Host: "127.0.0.1", Port: port, Channel: ChannelLeft}})
	waitFor(t, "ready", 8*time.Second, func() bool { return memberState(l, "a") == StateReady })
	src := &fakeSource{}
	l.Start(src)
	waitFor(t, "engaged", 2*time.Second, l.Engaged)
	d := startDAC(c.Fill)
	defer d.close()
	src.set(true)
	time.Sleep(2500 * time.Millisecond)

	// Pause: the source stops, the leader is told.
	src.mu.Lock()
	src.playing, src.paused = false, true
	src.mu.Unlock()
	time.Sleep(700 * time.Millisecond)
	_, before := d.samples()
	time.Sleep(700 * time.Millisecond)
	_, after := d.samples()
	if len(after) != len(before) {
		t.Errorf("%d samples played during the pause: the buffer ran on", len(after)-len(before))
	}
	last := before[len(before)-1]

	// Resume: what was buffered ahead of the pause plays first, so the music
	// carries on from where it was heard to stop rather than two seconds on.
	src.set(true)
	time.Sleep(2 * time.Second)
	_, vals := d.samples()
	resumed := vals[len(before)]
	gap := (int(resumed) - int(last) + sigPeriod) % sigPeriod
	if gap < 1 || gap > 6000 {
		t.Errorf("resumed %d samples after the pause point, want a few hundred at most", gap)
	}
	if n := breaks(vals[len(before):], nextL); n > 3 {
		t.Errorf("%d discontinuities after resuming", n)
	}

	// Skip: the old audio stops at once and the new starts after the lead.
	src.mu.Lock()
	src.track, src.pos = 1, 0
	src.disc = true
	src.mu.Unlock()
	time.Sleep(300 * time.Millisecond)
	_, mark := d.samples()
	time.Sleep(2500 * time.Millisecond)
	_, vals = d.samples()
	tail := vals[len(mark):]
	if len(tail) < 24000 {
		t.Fatalf("only %d samples of the new track played", len(tail))
	}
	for i, v := range tail {
		if v < 15000 {
			t.Fatalf("sample %d after the skip is still the old track (%d)", i, v)
		}
	}
}

func TestPlayerThatGoesBusyIsLeftAloneAndComesBack(t *testing.T) {
	if testing.Short() {
		t.Skip("real time")
	}
	c, port := startPlayer(t, "p")
	l := newTestLeader(t)
	share(t, l, c)
	l.SetMembers("G", ChannelLeft, []Member{{ID: "a", ClientID: c.ClientID(), Host: "127.0.0.1", Port: port, Channel: ChannelLeft}})
	waitFor(t, "ready", 8*time.Second, func() bool { return memberState(l, "a") == StateReady })

	c.SetExternal(true) // the Echo's own Spotify, or Home Assistant, took the speaker
	waitFor(t, "busy", 3*time.Second, func() bool { return memberState(l, "a") == StateBusy })
	c.SetExternal(false)
	waitFor(t, "ready again", 5*time.Second, func() bool { return memberState(l, "a") == StateReady })
}

// A server pointed at the Echo by hand takes it from the group and keeps it;
// the group comes back once that server lets go. At equal rank the two took
// the Echo from each other every 5s.
func TestAManualServerTakesAMemberFromTheGroupAndGivesItBack(t *testing.T) {
	if testing.Short() {
		t.Skip("real time")
	}
	c, port := startPlayer(t, "p")
	group := newTestLeader(t)
	share(t, group, c)
	m := []Member{{ID: "a", ClientID: c.ClientID(), Host: "127.0.0.1", Port: port}}
	group.SetMembers("G", ChannelMono, m)
	waitFor(t, "the group to hold the player", 8*time.Second, func() bool { return memberState(group, "a") == StateReady })

	tv := newTestLeader(t)
	tv.plainServer = true
	share(t, tv, c)
	tv.SetMembers("TV", ChannelMono, m)
	waitFor(t, "the manual server to take it", 8*time.Second, func() bool {
		return memberState(tv, "a") == StateReady && memberState(group, "a") == StateBusy
	})
	// Past the group's 5s retry: it must not take the player back.
	time.Sleep(busyBackoff + 2*time.Second)
	if memberState(tv, "a") != StateReady || memberState(group, "a") != StateBusy {
		t.Fatalf("after the group's retry: manual %q, group %q", memberState(tv, "a"), memberState(group, "a"))
	}

	tv.SetMembers("TV", ChannelMono, nil) // the TV disconnects
	waitFor(t, "the group to come back", busyBackoff+5*time.Second, func() bool { return memberState(group, "a") == StateReady })
}

// The other way round: a group's leader arriving finds the player held by a
// manual server and stays out.
func TestAGroupDoesNotTakeAMemberFromAManualServer(t *testing.T) {
	c, port := startPlayer(t, "p")
	m := []Member{{ID: "a", ClientID: c.ClientID(), Host: "127.0.0.1", Port: port}}
	tv := newTestLeader(t)
	tv.plainServer = true
	share(t, tv, c)
	tv.SetMembers("TV", ChannelMono, m)
	waitFor(t, "the manual server to hold the player", 8*time.Second, func() bool { return memberState(tv, "a") == StateReady })

	group := newTestLeader(t)
	share(t, group, c)
	group.SetMembers("G", ChannelMono, m)
	waitFor(t, "the group turned away", 8*time.Second, func() bool { return memberState(group, "a") == StateBusy })
	if memberState(tv, "a") != StateReady {
		t.Fatalf("the group displaced the manual server: %q", memberState(tv, "a"))
	}
}

// The Echo's own Spotify keeps a group out (someone picked that Echo by
// itself) but not a manual server, which the music mux lets take over.
func TestOwnSpotifyTurnsAwayAGroupButNotAManualServer(t *testing.T) {
	c, port := startPlayer(t, "p")
	c.SetOwnMusic(true)
	m := []Member{{ID: "a", ClientID: c.ClientID(), Host: "127.0.0.1", Port: port}}
	group := newTestLeader(t)
	share(t, group, c)
	group.SetMembers("G", ChannelMono, m)
	waitFor(t, "the group to see the Echo busy", 8*time.Second, func() bool { return memberState(group, "a") == StateBusy })

	group.SetMembers("G", ChannelMono, nil)
	tv := newTestLeader(t)
	tv.plainServer = true
	share(t, tv, c)
	tv.SetMembers("TV", ChannelMono, m)
	waitFor(t, "the manual server admitted and told it may play", 8*time.Second, func() bool {
		return memberState(tv, "a") == StateReady && c.Status().State != "busy"
	})
}

func TestVolumeReachesThePlayerOnceAndOnJoining(t *testing.T) {
	c, port := startPlayer(t, "p")
	var mu sync.Mutex
	var got []int
	c.OnVolume(func(v int) { mu.Lock(); got = append(got, v); mu.Unlock() })
	l := newTestLeader(t)
	share(t, l, c)
	l.SetVolume(37) // set before the player is there: it is told on joining
	l.SetMembers("G", ChannelMono, []Member{{ID: "a", ClientID: c.ClientID(), Host: "127.0.0.1", Port: port}})
	waitFor(t, "ready", 8*time.Second, func() bool { return memberState(l, "a") == StateReady })
	waitFor(t, "volume", 3*time.Second, func() bool { mu.Lock(); defer mu.Unlock(); return len(got) == 1 })
	l.SetVolume(37) // unchanged: not sent again
	l.SetVolume(60)
	waitFor(t, "second volume", 3*time.Second, func() bool { mu.Lock(); defer mu.Unlock(); return len(got) == 2 })
	mu.Lock()
	defer mu.Unlock()
	if got[0] != 37 || got[1] != 60 {
		t.Errorf("player told %v, want [37 60]", got)
	}
}

func TestRemovingAMemberSaysGoodbyeAndStopsDialling(t *testing.T) {
	c, port := startPlayer(t, "p")
	l := newTestLeader(t)
	share(t, l, c)
	l.SetMembers("G", ChannelMono, []Member{{ID: "a", ClientID: c.ClientID(), Host: "127.0.0.1", Port: port}})
	waitFor(t, "ready", 8*time.Second, func() bool { return memberState(l, "a") == StateReady })
	l.SetMembers("G", ChannelMono, nil)
	if n := len(l.Status().Members); n != 0 {
		t.Fatalf("%d members after removal", n)
	}
	waitFor(t, "the player to see it leave", 3*time.Second, func() bool { return c.Status().State == "listening" })
}

func TestNothingRunsUntilThereIsAMember(t *testing.T) {
	l := newTestLeader(t)
	src := &fakeSource{}
	l.Start(src)
	src.set(true)
	time.Sleep(300 * time.Millisecond)
	if l.Engaged() || l.Active() {
		t.Error("engaged with nobody to lead: the Echo would have stopped playing on its own")
	}
	src.mu.Lock()
	reads := src.reads
	src.mu.Unlock()
	if reads != 0 {
		t.Errorf("the leader consumed %d periods of audio it had no use for", reads)
	}
}
