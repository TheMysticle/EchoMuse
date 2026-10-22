package spotify

import (
	"bufio"
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// The test binary doubles as the stand-in engine process: it just waits.
func TestMain(m *testing.M) {
	if os.Getenv("EM_FAKE_ENGINE") == "1" {
		time.Sleep(time.Minute)
		return
	}
	os.Setenv("EM_FAKE_ENGINE", "1")
	os.Exit(m.Run())
}

// fakeDaemon dials the player's sockets the way the engine does.
func fakeDaemon(t *testing.T, dir string) (ctl net.Conn, aud net.Conn) {
	t.Helper()
	var err error
	for i := 0; i < 50; i++ {
		ctl, err = net.Dial("unix", filepath.Join(dir, "control.sock"))
		if err == nil {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if err != nil {
		t.Fatal(err)
	}
	aud, err = net.Dial("unix", filepath.Join(dir, "audio.sock"))
	if err != nil {
		t.Fatal(err)
	}
	return
}

func waitFor(t *testing.T, what string, f func() bool) {
	t.Helper()
	for i := 0; i < 200; i++ {
		if f() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func startPlayer(t *testing.T) (*Player, string) {
	t.Helper()
	dir := t.TempDir()
	exe, _ := os.Executable()
	p := New(Config{Binary: exe, CacheDir: filepath.Join(dir, "c"), SocketDir: filepath.Join(dir, "s"), Name: "t", DeviceID: "d"})
	if err := p.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(p.Stop)
	return p, filepath.Join(dir, "s")
}

func TestEventsUpdateStatus(t *testing.T) {
	p, dir := startPlayer(t)
	ctl, _ := fakeDaemon(t, dir)
	send := func(v any) {
		b, _ := json.Marshal(v)
		ctl.Write(append(b, '\n'))
	}
	send(map[string]any{"event": "link", "status": "linked"})
	send(map[string]any{"event": "session", "connected": true, "user": "u"})
	send(map[string]any{"event": "state", "state": "playing", "position_ms": 1500})
	send(map[string]any{"event": "track", "uri": "spotify:track:x", "name": "N", "artists": []string{"A"}})
	waitFor(t, "track", func() bool { s := p.Status(); return s.Track != nil && s.State == "playing" })
	s := p.Status()
	if s.Link != "linked" || !s.Connected || s.User != "u" || s.Track.Name != "N" {
		t.Fatalf("status = %+v", s)
	}
	if !p.Active() {
		t.Fatal("playing engine must be Active")
	}
}

func TestNonLinkedSessionIsNotConnected(t *testing.T) {
	p, dir := startPlayer(t)
	ctl, _ := fakeDaemon(t, dir)
	ctl.Write([]byte(`{"event":"session","connected":true,"user":"u"}` + "\n"))
	waitFor(t, "connected", func() bool { return p.Status().Connected })
	ctl.Write([]byte(`{"event":"link","status":"auth_failed","detail":"bad"}` + "\n"))
	waitFor(t, "link", func() bool { return p.Status().Link == "auth_failed" })
	if p.Status().Connected {
		t.Fatal("auth_failed must not read as connected")
	}
}

// The old test here was named ...AndShortReadIsPadded and asserted the
// defect: a partial period zero-padded and reported as success. That put
// silence into the middle of the waveform — a click — and no counter could
// see it, because the period was delivered on time and full length. Four
// instruments reported nothing while a Radar was plainly glitching
// (2026-10-11). A test that pins a bug is worse than no test, because it is
// cited as the reason not to change the thing.
func TestAWholePeriodOrNothingAndTheRestStaysQueued(t *testing.T) {
	p, dir := startPlayer(t)
	_, aud := fakeDaemon(t, dir)
	const period = 8
	prime := primePeriods * period

	aud.Write([]byte{1, 2, 3, 4, 5, 6})
	waitFor(t, "ring", func() bool { return p.ring.len() == 6 })
	out := []byte{9, 9, 9, 9, 9, 9, 9, 9}
	if p.Fill(out, time.Time{}, 0) {
		t.Fatal("a partial period was delivered; it would be padded with silence")
	}
	if p.ring.len() != 6 {
		t.Fatalf("declining consumed %d bytes; they must stay queued so the "+
			"waveform resumes intact", 6-p.ring.len())
	}

	// Enough to prime. The first period must be the original bytes in order,
	// with nothing spliced in.
	rest := make([]byte, prime-6)
	for i := range rest {
		rest[i] = byte(7 + i)
	}
	aud.Write(rest)
	waitFor(t, "primed", func() bool { return p.ring.len() == prime })
	if !p.Fill(out, time.Time{}, 0) {
		t.Fatal("a primed ring did not deliver a period")
	}
	for i := range out {
		if out[i] != byte(1+i) {
			t.Fatalf("out = %v, want 1..%d — the stream is not contiguous",
				out, period)
		}
	}
}

func TestAGapRequiresARefillBeforeReadingResumes(t *testing.T) {
	// Without this the buffer oscillates around a period boundary and every
	// other period is a gap — a run of clicks instead of one.
	p, dir := startPlayer(t)
	_, aud := fakeDaemon(t, dir)
	const period = 8
	prime := primePeriods * period
	out := make([]byte, period)

	aud.Write(make([]byte, prime))
	waitFor(t, "primed", func() bool { return p.ring.len() == prime })
	for i := 0; i < primePeriods; i++ {
		if !p.Fill(out, time.Time{}, 0) {
			t.Fatalf("period %d of a primed ring was declined", i)
		}
	}
	if p.Fill(out, time.Time{}, 0) {
		t.Fatal("an empty ring delivered a period")
	}
	// One period back is NOT enough to resume: it must refill to prime.
	aud.Write(make([]byte, period))
	waitFor(t, "one period", func() bool { return p.ring.len() == period })
	if p.Fill(out, time.Time{}, 0) {
		t.Fatal("reading resumed on one period; a refill to prime is required")
	}
	aud.Write(make([]byte, prime-period))
	waitFor(t, "refilled", func() bool { return p.ring.len() == prime })
	if !p.Fill(out, time.Time{}, 0) {
		t.Fatal("a refilled ring did not resume")
	}
}

func TestCommandsReachTheEngineAndFlushOnLoad(t *testing.T) {
	p, dir := startPlayer(t)
	ctl, aud := fakeDaemon(t, dir)
	r := bufio.NewReader(ctl)
	// readEvents may send an initial volume; give it a status first.
	aud.Write([]byte{1, 2, 3, 4})
	waitFor(t, "ring", func() bool { return p.ring.len() == 4 })
	waitFor(t, "ctl", func() bool { return p.Send("status", nil) == nil })
	if err := p.Send("load", map[string]any{"uri": "spotify:album:y"}); err != nil {
		t.Fatal(err)
	}
	if p.ring.len() != 0 {
		t.Fatal("load must drop queued audio")
	}
	ctl.SetReadDeadline(time.Now().Add(2 * time.Second))
	var got map[string]any
	for {
		line, err := r.ReadBytes('\n')
		if err != nil {
			t.Fatal(err)
		}
		json.Unmarshal(line, &got)
		if got["cmd"] == "load" {
			break
		}
	}
	if got["uri"] != "spotify:album:y" {
		t.Fatalf("got %v", got)
	}
}

func TestSendWithoutEngineIsAnError(t *testing.T) {
	p := New(Config{})
	if p.Send("play", nil) == nil {
		t.Fatal("expected error")
	}
}

func TestMissingBinaryIsReported(t *testing.T) {
	p := New(Config{Binary: "/nonexistent/em-spotify", SocketDir: t.TempDir()})
	if p.Start() == nil || p.Status().Engine != EngineMissing {
		t.Fatalf("status = %+v", p.Status())
	}
}

func TestPremiumLine(t *testing.T) {
	if !premiumLine("ERROR librespot: Free user not supported") {
		t.Fatal("missed")
	}
	if premiumLine("INFO connected") {
		t.Fatal("false positive")
	}
}

// The volume conversion must be a FIXED POINT in both directions.
//
// The level crosses this boundary both ways, so a value that merely passes
// through and comes back changed is a change nobody made — and librespot
// echoes a device-set level straight back, so each lap applied the new value
// and started another. Truncating lost a step on 95 of 101 values, always
// downward: the volume walked itself down during playback.
func TestVolumeRoundTripsExactly(t *testing.T) {
	for pct := 0; pct <= 100; pct++ {
		if got := U16ToPercent(PercentToU16(pct)); got != pct {
			t.Errorf("percent %d round-tripped to %d (via u16 %d)",
				pct, got, PercentToU16(pct))
		}
	}
}

func TestVolumeEndsAreExact(t *testing.T) {
	// 0 must be silence and 100 must be the top. A near-miss at either end
	// is a device that cannot be silenced, or cannot reach full volume.
	if got := PercentToU16(0); got != 0 {
		t.Errorf("0%% is u16 %d, want 0", got)
	}
	if got := PercentToU16(100); got != 65535 {
		t.Errorf("100%% is u16 %d, want 65535", got)
	}
	if got := U16ToPercent(0); got != 0 {
		t.Errorf("u16 0 is %d%%, want 0", got)
	}
	if got := U16ToPercent(65535); got != 100 {
		t.Errorf("u16 65535 is %d%%, want 100", got)
	}
}

func TestVolumeIsClampedRatherThanWrapped(t *testing.T) {
	// uint16(pct * 65535 / 100) wraps for anything over 100, so a bad value
	// would come out LOUD rather than merely wrong.
	for _, pct := range []int{-1, -1000, 101, 1000} {
		got := PercentToU16(pct)
		if pct < 0 && got != 0 {
			t.Errorf("percent %d gave u16 %d, want 0", pct, got)
		}
		if pct > 100 && got != 65535 {
			t.Errorf("percent %d gave u16 %d, want 65535", pct, got)
		}
	}
}

func TestVolumeIsMonotonic(t *testing.T) {
	// Rounding must not make a higher percentage quieter anywhere.
	for pct := 1; pct <= 100; pct++ {
		if PercentToU16(pct) <= PercentToU16(pct-1) {
			t.Errorf("percent %d is not louder than %d", pct, pct-1)
		}
	}
}

func TestBinaryChangedOnlyOnEvidence(t *testing.T) {
	// The restart this drives cuts the music and drops the Spotify link, so
	// it must happen on a known change and nothing else. An unreadable stamp
	// is the case that matters: os.Stat failing is not a new binary.
	cases := []struct {
		name, started, current string
		want                   bool
	}{
		{"a new binary was installed", "100:7", "104:9", true},
		{"the same binary", "100:7", "100:7", false},
		{"the running stamp is unknown", "", "104:9", false},
		{"the current stamp is unreadable", "100:7", "", false},
		{"neither is known", "", "", false},
	}
	for _, c := range cases {
		if got := BinaryChanged(c.started, c.current); got != c.want {
			t.Errorf("%s: BinaryChanged(%q, %q) = %v, want %v",
				c.name, c.started, c.current, got, c.want)
		}
	}
}

func TestATailThatCannotPlayIsDroppedSoTheSourceGoesIdle(t *testing.T) {
	// After the engine stopped with less than the prime left in the ring, Fill
	// declined for ever while Active stayed true, so the speaker kept this
	// source as the music owner and a group member reported itself busy.
	p, dir := startPlayer(t)
	_, aud := fakeDaemon(t, dir)
	old := strandedAfter
	strandedAfter = 20 * time.Millisecond
	defer func() { strandedAfter = old }()
	const period = 8

	aud.Write(make([]byte, period*2))
	waitFor(t, "tail", func() bool { return p.ring.len() == period*2 })
	out := make([]byte, period)
	for i := 0; i < 100 && p.Active(); i++ {
		p.Fill(out, time.Time{}, 0)
		time.Sleep(time.Millisecond)
	}
	if p.Active() {
		t.Fatal("a tail below the prime kept the source active for good")
	}
}

func TestThePauseIsRepeatedWhileTheEngineKeepsPlayingUnderAnotherSource(t *testing.T) {
	t0 := time.Unix(1000, 0)
	if !PauseDue(true, true, time.Time{}, t0) {
		t.Fatal("no pause for an engine playing under a taken plane")
	}
	if PauseDue(true, true, t0, t0.Add(pauseRetry/2)) {
		t.Fatal("pause resent before the retry interval")
	}
	if !PauseDue(true, true, t0, t0.Add(pauseRetry)) {
		t.Fatal("a pause the engine ignored was not repeated")
	}
	if PauseDue(true, false, t0, t0.Add(10*time.Second)) || PauseDue(false, true, time.Time{}, t0) {
		t.Fatal("paused an engine that was not playing, or when nothing had the plane")
	}
}
