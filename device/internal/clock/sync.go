package clock

import (
	"context"
	"encoding/binary"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/wilbowes/EchoMuse/internal/atomicfile"
)

var confirmed atomic.Bool
var lastController atomic.Int64

var restoreTrust sync.Once

const trustFile = "/data/local/etc/echomuse/clock-trust"

func sameBoot(record, current string) bool {
	return strings.TrimSpace(current) != "" && strings.TrimSpace(record) == strings.TrimSpace(current)
}
func Trusted() bool {
	restoreTrust.Do(func() {
		record, e := os.ReadFile(trustFile)
		boot, e2 := os.ReadFile("/proc/sys/kernel/random/boot_id")
		if e == nil && e2 == nil && sameBoot(string(record), string(boot)) && time.Now().Year() >= 2024 {
			confirmed.Store(true)
		}
	})
	return confirmed.Load()
}

// A daemon restart during the same kernel boot can retain clock confidence.
// The kernel generates a different boot ID on every reboot, so this marker
// can never assert that powered-off time was measured by an absent RTC.
func confirmTime() {
	confirmed.Store(true)
	notify()
	boot, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil || strings.TrimSpace(string(boot)) == "" {
		return
	}
	if os.MkdirAll(filepath.Dir(trustFile), 0700) != nil {
		return
	}
	_ = atomicfile.Write(trustFile, boot, 0600)
}

// ConfirmController is called only after an authenticated/approved ack and a
// successful clock correction (or an already-close clock).
func ConfirmController(ms int64) {
	if ms > 1704067200000 {
		confirmTime()
		lastController.Store(time.Now().Unix())
	}
}

// SyncNetwork is a bounded client-only SNTP fallback. Go's resolver works on
// emOS. A recent controller sync wins; no new inbound listener is introduced.
func SyncNetwork(ctx context.Context, servers []string) {
	for {
		if time.Now().Unix()-lastController.Load() > 3600 {
			for _, host := range servers {
				if syncHost(ctx, host) == nil {
					break
				}
			}
		}
		t := time.NewTimer(5 * time.Minute)
		select {
		case <-ctx.Done():
			t.Stop()
			return
		case <-t.C:
		}
	}
}
func syncHost(ctx context.Context, host string) error {
	c, e := (&net.Dialer{Timeout: 3 * time.Second}).DialContext(ctx, "udp", net.JoinHostPort(host, "123"))
	if e != nil {
		return e
	}
	defer c.Close()
	_ = c.SetDeadline(time.Now().Add(3 * time.Second))
	b := make([]byte, 48)
	b[0] = 0x23
	sent := time.Now()
	binary.BigEndian.PutUint64(b[40:], uint64(sent.Unix()+2208988800)<<32|uint64(sent.Nanosecond())*(1<<32)/1000000000)
	stamp := append([]byte(nil), b[40:]...)
	if _, e = c.Write(b); e != nil {
		return e
	}
	n, e := c.Read(b)
	if e != nil {
		return e
	}
	if n < 48 || b[0]>>6 == 3 || b[0]&7 != 4 || b[1] == 0 || b[1] > 15 {
		return &net.AddrError{Err: "invalid NTP response", Addr: host}
	}
	for n := 0; n < 8; n++ {
		if b[24+n] != stamp[n] {
			return &net.AddrError{Err: "NTP origin mismatch", Addr: host}
		}
	}
	sec := int64(binary.BigEndian.Uint32(b[40:44])) - 2208988800
	if sec < 1704067200 {
		sec += 1 << 32
	} // NTP era rollover in 2036.
	ns := int64(binary.BigEndian.Uint32(b[44:48])) * 1000000000 >> 32
	t := time.Unix(sec, ns).Add(time.Since(sent) / 2)
	if t.Year() < 2024 || t.Year() > 2100 {
		return &net.AddrError{Err: "implausible NTP era", Addr: host}
	}
	if e = Step(t.UnixMilli()); e == nil {
		confirmTime()
	}
	return e
}
