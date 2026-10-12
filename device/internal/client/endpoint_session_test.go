package client

import (
	"encoding/binary"
	"math/rand"
	"testing"

	"github.com/wilbowes/EchoMuse/internal/beamformer"
)

// chunk80 is 80ms of mono S16LE noise at the given rms.
func chunk80(rng *rand.Rand, rms float64) []byte {
	b := make([]byte, 1280*2)
	for i := 0; i < 1280; i++ {
		v := int16(rng.NormFloat64() * rms)
		binary.LittleEndian.PutUint16(b[i*2:], uint16(v))
	}
	return b
}

// The glue between the wake stream and the endpointer, driven as the mic loop
// does: every chunk goes to the endpointer, and endpointSession is told how
// many frames the listen gate released for it.
func driveSession(t *testing.T, on bool) (sends int, firstSendMs int) {
	t.Helper()
	d := &DataClient{endpoint: beamformer.NewEndpointer()}
	rng := rand.New(rand.NewSource(7))
	var cur uint32
	var sent bool
	var tick int
	firstSendMs = -1
	send := func() {
		sends++
		if firstSendMs < 0 {
			firstSendMs = -2 // replaced below by the caller's clock
		}
	}
	ms := 0
	step := func(rms float64, frames int) {
		c := chunk80(rng, rms)
		if on {
			d.endpointMu.Lock()
			d.endpoint.Push(c)
			d.endpointMu.Unlock()
		}
		before := sends
		d.endpointSession(on, frames, 1, &cur, &sent, &tick, send)
		if sends > before && firstSendMs == -2 {
			firstSendMs = ms
		}
		ms += 80
	}
	for i := 0; i < 150; i++ { // 12s of TV alone: the room it is judged against
		step(900, 0)
	}
	step(900, 1) // the session opens
	talkFrom := ms
	for i := 0; i < 25; i++ { // 2s of a talker well over the TV
		step(5000, 2)
	}
	talkTo := ms
	for i := 0; i < 50; i++ { // 4s of TV again, session still open
		step(900, 2)
	}
	_ = talkFrom
	if firstSendMs >= 0 {
		firstSendMs -= talkTo
	}
	return
}

func TestTheEndIsSentOnceAboutASecondAfterTheTalkerStops(t *testing.T) {
	sends, after := driveSession(t, true)
	if sends != 1 {
		t.Fatalf("end of speech sent %d times, want once", sends)
	}
	if after < 800 || after > 1700 {
		t.Errorf("sent %dms after the talker stopped, want about 1s", after)
	}
}

func TestNothingIsSentWhenTheSettingIsOff(t *testing.T) {
	if sends, _ := driveSession(t, false); sends != 0 {
		t.Errorf("sent %d end markers with wakeEndpoint off", sends)
	}
}
