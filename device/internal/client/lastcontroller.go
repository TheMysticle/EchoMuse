package client

import (
	"encoding/json"
	"fmt"
	"log"
	"os"

	"github.com/wilbowes/EchoMuse/internal/atomicfile"
	"github.com/wilbowes/EchoMuse/internal/discovery"
)

// lastControllerPath keeps the controller this device last connected to,
// across restarts of the firmware. The reconnect fast path (try the last
// address before mDNS) lived in memory only, so every restart, an OTA
// included, depended on mDNS, and an Echo that had stopped hearing multicast
// could not come back from one without a power cut (2026-10-26). It is the
// address only: what the link may do there is still decided by the TLS
// credentials and the controller's token check on every dial, exactly as for
// an address mDNS returned.
var lastControllerPath = "/data/local/etc/echomuse/last_controller.json"

type lastController struct {
	Host    string `json:"host"`
	Port    int    `json:"port"`
	TLSPort int    `json:"tls_port,omitempty"`
}

func loadLastController() *discovery.ServerInfo {
	b, err := os.ReadFile(lastControllerPath)
	if err != nil {
		return nil
	}
	var lc lastController
	if json.Unmarshal(b, &lc) != nil || lc.Host == "" || lc.Port <= 0 || lc.Port > 65535 {
		return nil
	}
	return &discovery.ServerInfo{
		Host:    lc.Host,
		Port:    lc.Port,
		Addr:    fmt.Sprintf("%s:%d", lc.Host, lc.Port),
		TLSPort: lc.TLSPort,
	}
}

// saveLastController writes s when it differs from what is on disk; a
// reconnect to the same controller writes nothing.
func saveLastController(s *discovery.ServerInfo) {
	if s == nil || s.Host == "" {
		return
	}
	if cur := loadLastController(); cur != nil && cur.Host == s.Host && cur.Port == s.Port && cur.TLSPort == s.TLSPort {
		return
	}
	b, _ := json.Marshal(lastController{Host: s.Host, Port: s.Port, TLSPort: s.TLSPort})
	if err := atomicfile.Write(lastControllerPath, b, 0o600); err != nil {
		log.Printf("[control] could not remember the controller address: %v", err)
	}
}
