package sendspin

import (
	"log"
	"net"
	"sync"
	"syscall"

	"github.com/gorilla/websocket"
)

// Immediate ACKs on the audio connection.
//
// A live stream arrives as one small segment per chunk (~600 bytes every
// 10ms), and Linux ACKs every second segment once it has learned that size,
// so the server sees every ACK one chunk late: 12.3ms smoothed RTT on a link
// that pings at 1-2ms (Radar, 2026-10-24). The server's loss recovery runs
// off that RTT — RACK's reorder window, the tail-loss probe at 2xRTT — so a
// single segment lost in the air is resent ~25-35ms later rather than ~10ms,
// which is the size of stall a 65-70ms lead fails to ride out.
//
// TCP_QUICKACK is not sticky: the kernel drops back to delayed ACKs on its
// own, and setting it ACKs whatever is pending. So it is re-armed after every
// read, the usual idiom; the reader wakes as each segment lands, so each one
// is ACKed as it is consumed. The cost is one setsockopt per message and one
// ACK per chunk instead of per two — ~50 more 40-byte frames a second up.
type quickAcker struct {
	raw syscall.RawConn
}

var quickAckWarnOnce sync.Once

func newQuickAcker(ws *websocket.Conn) quickAcker {
	tc, ok := ws.UnderlyingConn().(*net.TCPConn)
	if !ok {
		return quickAcker{}
	}
	raw, err := tc.SyscallConn()
	if err != nil {
		return quickAcker{}
	}
	return quickAcker{raw: raw}
}

// arm is best-effort: a failure leaves delayed ACKs, the old behaviour.
func (q quickAcker) arm() {
	if q.raw == nil {
		return
	}
	var serr error
	if err := q.raw.Control(func(fd uintptr) {
		serr = syscall.SetsockoptInt(int(fd), syscall.IPPROTO_TCP, syscall.TCP_QUICKACK, 1)
	}); err != nil {
		serr = err
	}
	if serr != nil {
		quickAckWarnOnce.Do(func() {
			log.Printf("[sendspin] TCP_QUICKACK not applied (%v) — ACKs stay delayed", serr)
		})
	}
}
