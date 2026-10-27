//go:build server

// dac_probe: records what an Echo's own output path is playing, from an
// ALSA capture tap that runs beside the firmware, with every buffer stamped
// on a clock shared with a host. Two Echoes captured this way can be lined
// up sample for sample, which says how far apart their speakers actually
// are — something neither Echo can see from its own sync statistics.
//
// Usage:
//   dac_probe level <card> <device>
//       one second from the tap, then its RMS: finds the tap that carries
//       the speaker's audio.
//   dac_probe capture <card> <device> <seconds> <host:udpport> <host:tcpport> <label>
//       sync to the host's UDP time server, capture, sync again, and send
//       {meta JSON line}{raw S16LE stereo 48kHz} to the host's TCP sink.
//
// The host side is a few lines of Python (see the session notes); it answers
// each 8-byte UDP datagram with the datagram plus its own nanosecond clock.
//
// Build: tools/build_tools.sh (or the same docker run with this directory).

package main

import (
	"bufio"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"log"
	"math"
	"net"
	"os"
	"sort"
	"strconv"
	"time"

	"github.com/Binozo/GoTinyAlsa/pkg/pcm"
	"github.com/Binozo/GoTinyAlsa/pkg/tinyalsa"
)

const (
	rate        = 48000
	channels    = 2
	periodSize  = 256
	periodCount = 8
)

var start = time.Now()

// mono is this process's monotonic clock in ns.
func mono() int64 { return int64(time.Since(start)) }

func open(card, dev int) tinyalsa.AlsaDevice {
	return tinyalsa.NewDevice(card, dev, pcm.Config{
		Channels: channels, SampleRate: rate, PeriodSize: periodSize,
		PeriodCount: periodCount, Format: tinyalsa.PCM_FORMAT_S16_LE,
	})
}

type read struct {
	At    int64 `json:"at"`    // mono ns when the buffer arrived
	Bytes int   `json:"bytes"` // its size
}

// capture runs the tap for d and returns the audio with arrival stamps.
func capture(card, dev int, d time.Duration) ([]byte, []read) {
	device := open(card, dev)
	stream := make(chan []byte, 64)
	errCh := make(chan error, 1)
	go func() { errCh <- device.GetAudioStream(device.DeviceConfig, stream) }()
	var audio []byte
	var reads []read
	deadline := time.After(d)
	for {
		select {
		case <-deadline:
			return audio, reads
		case err := <-errCh:
			if err != nil {
				log.Fatalf("capture: %v", err)
			}
			return audio, reads
		case b := <-stream:
			reads = append(reads, read{At: mono(), Bytes: len(b)})
			audio = append(audio, b...)
		}
	}
}

type sample struct {
	Rtt    int64 `json:"rtt"`    // ns
	Offset int64 `json:"offset"` // host clock minus mono, ns, at the midpoint
}

// syncHost exchanges n datagrams and returns them all; the analysis keeps
// the fastest, whose midpoint is the least ambiguous.
func syncHost(addr string, n int) []sample {
	c, err := net.Dial("udp", addr)
	if err != nil {
		log.Fatalf("sync: %v", err)
	}
	defer c.Close()
	out := make([]sample, 0, n)
	buf := make([]byte, 64)
	for i := 0; i < n; i++ {
		t0 := mono()
		var req [8]byte
		binary.LittleEndian.PutUint64(req[:], uint64(t0))
		if _, err := c.Write(req[:]); err != nil {
			continue
		}
		c.SetReadDeadline(time.Now().Add(200 * time.Millisecond))
		m, err := c.Read(buf)
		t1 := mono()
		if err != nil || m < 16 || int64(binary.LittleEndian.Uint64(buf[:8])) != t0 {
			continue
		}
		host := int64(binary.LittleEndian.Uint64(buf[8:16]))
		out = append(out, sample{Rtt: t1 - t0, Offset: host - (t0+t1)/2})
		time.Sleep(5 * time.Millisecond)
	}
	return out
}

func best(s []sample) (int64, int64) {
	c := append([]sample(nil), s...)
	sort.Slice(c, func(i, j int) bool { return c[i].Rtt < c[j].Rtt })
	return c[0].Rtt, c[0].Offset
}

func rms(b []byte) float64 {
	var sum float64
	n := len(b) / 2
	for i := 0; i+1 < len(b); i += 2 {
		v := float64(int16(binary.LittleEndian.Uint16(b[i:])))
		sum += v * v
	}
	if n == 0 {
		return -120
	}
	r := math.Sqrt(sum/float64(n)) / 32768
	if r == 0 {
		return -120
	}
	return 20 * math.Log10(r)
}

func atoi(s string) int {
	n, err := strconv.Atoi(s)
	if err != nil {
		log.Fatalf("not a number: %q", s)
	}
	return n
}

func main() {
	if len(os.Args) < 4 {
		log.Fatalf("usage: dac_probe level <card> <dev> | capture <card> <dev> <s> <udp> <tcp> <label>")
	}
	card, dev := atoi(os.Args[2]), atoi(os.Args[3])
	switch os.Args[1] {
	case "level":
		audio, reads := capture(card, dev, time.Second)
		fmt.Printf("card %d dev %d: %d reads, %d bytes, %.1f dBFS\n", card, dev, len(reads), len(audio), rms(audio))
	case "capture":
		if len(os.Args) < 8 {
			log.Fatalf("capture needs <seconds> <host:udp> <host:tcp> <label>")
		}
		secs, udp, tcp, label := atoi(os.Args[4]), os.Args[5], os.Args[6], os.Args[7]
		before := syncHost(udp, 200)
		audio, reads := capture(card, dev, time.Duration(secs)*time.Second)
		after := syncHost(udp, 200)
		r0, o0 := best(before)
		r1, o1 := best(after)
		fmt.Printf("sync before: rtt %.2fms, after: rtt %.2fms, offset moved %.3fms; %d reads, %.1f dBFS\n",
			float64(r0)/1e6, float64(r1)/1e6, float64(o1-o0)/1e6, len(reads), rms(audio))
		meta, _ := json.Marshal(map[string]any{
			"label": label, "rate": rate, "channels": channels,
			"before": before, "after": after, "reads": reads,
		})
		c, err := net.Dial("tcp", tcp)
		if err != nil {
			log.Fatalf("send: %v", err)
		}
		w := bufio.NewWriter(c)
		w.Write(meta)
		w.WriteByte('\n')
		w.Write(audio)
		w.Flush()
		c.Close()
		fmt.Printf("sent %d bytes of audio to %s\n", len(audio), tcp)
	default:
		log.Fatalf("unknown mode %q", os.Args[1])
	}
}
