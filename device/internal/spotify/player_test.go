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

func TestAudioReachesFillAndShortReadIsPadded(t *testing.T) {
	p, dir := startPlayer(t)
	_, aud := fakeDaemon(t, dir)
	aud.Write([]byte{1, 2, 3, 4, 5, 6})
	waitFor(t, "ring", func() bool { return p.ring.len() == 6 })
	out := []byte{9, 9, 9, 9, 9, 9, 9, 9}
	if !p.Fill(out, time.Time{}, 0) {
		t.Fatal("Fill reported nothing")
	}
	if string(out) != string([]byte{1, 2, 3, 4, 5, 6, 0, 0}) {
		t.Fatalf("out = %v", out)
	}
	if p.Fill(out, time.Time{}, 0) {
		t.Fatal("empty ring must report no audio")
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
