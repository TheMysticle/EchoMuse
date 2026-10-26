package client

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/discovery"
)

func TestLastControllerSurvivesARestart(t *testing.T) {
	lastControllerPath = filepath.Join(t.TempDir(), "last_controller.json")
	t.Cleanup(func() { lastControllerPath = "/data/local/etc/echomuse/last_controller.json" })

	var c ControlClient
	if c.lastKnownServer() != nil {
		t.Fatal("an address with nothing saved")
	}
	saveLastController(&discovery.ServerInfo{Host: "192.168.1.47", Port: 8767, TLSPort: 8770})

	var next ControlClient // a new run of the firmware: nothing in memory
	s := next.lastKnownServer()
	if s == nil || s.Addr != "192.168.1.47:8767" || s.TLSPort != 8770 {
		t.Fatalf("after a restart: %+v", s)
	}

	st, _ := os.Stat(lastControllerPath)
	saveLastController(&discovery.ServerInfo{Host: "192.168.1.47", Port: 8767, TLSPort: 8770})
	if st2, _ := os.Stat(lastControllerPath); !st2.ModTime().Equal(st.ModTime()) {
		t.Error("the same controller was written again")
	}

	for _, bad := range []string{`{}`, `{"host":"x","port":0}`, `{"host":"x","port":70000}`, `not json`} {
		os.WriteFile(lastControllerPath, []byte(bad), 0o600)
		if s := loadLastController(); s != nil {
			t.Errorf("%s read as %+v", bad, s)
		}
	}
}
