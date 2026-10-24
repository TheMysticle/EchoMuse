package sendspin

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"syscall"
	"testing"

	"github.com/gorilla/websocket"
)

// The acker must find the TCP socket under a server-side WebSocket, which is
// how a session receives one; a nil raw conn would make arm a silent no-op.
func TestQuickAckerFindsTheSocketAndArms(t *testing.T) {
	got := make(chan quickAcker, 1)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ws, err := (&websocket.Upgrader{}).Upgrade(w, r, nil)
		if err != nil {
			t.Error(err)
			return
		}
		got <- newQuickAcker(ws)
		ws.ReadMessage()
	}))
	defer srv.Close()
	c, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(srv.URL, "http"), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	q := <-got
	if q.raw == nil {
		t.Fatal("no raw conn under the server WebSocket")
	}
	var serr error
	q.raw.Control(func(fd uintptr) {
		serr = syscall.SetsockoptInt(int(fd), syscall.IPPROTO_TCP, syscall.TCP_QUICKACK, 1)
	})
	if serr != nil {
		t.Fatalf("TCP_QUICKACK: %v", serr)
	}
	q.arm()
	quickAcker{}.arm() // no socket: a no-op, not a panic
}
