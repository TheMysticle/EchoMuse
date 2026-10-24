package sendspin

import (
	"log"
	"math"
	"sync"
	"sync/atomic"
	"time"
)

// epoch anchors the client clock. time.Time carries a monotonic reading, so
// Sub between two of them is immune to the wall clock being stepped — which
// the controller does to every Echo on connect (time_ms on the ack).
var epoch = time.Now()

func nowUs() int64 { return time.Since(epoch).Microseconds() }

func toUs(t time.Time) int64 { return t.Sub(epoch).Microseconds() }

// Sync correction, after the spec's suggested strategy: whole-frame drops and
// repeats, bit-exact everywhere else.
const (
	// deadbandFrames: errors under ~100µs are left alone.
	deadbandFrames = 5
	// maxStepFrames per period: 8 of 2048 is 0.39% speed, inside the spec's
	// 0.5% cap. The DAC's own crystal runs -0.056% (1.15 frames a period,
	// measured 2026-08-10), so this keeps up with room to spare.
	maxStepFrames = 8
	// stepPeriodFrames is the period maxStepFrames is per. A half-length
	// period gets half the step, so the correction rate stays 0.39%.
	stepPeriodFrames = 2048
	// snapUs: past this the error is a disturbance rather than drift, and
	// one-shot resync beats a second of audible speed change.
	snapUs = 5000
)

type chunk struct {
	ts  int64 // server µs of the first sample
	pcm []int16
}

// player holds decoded audio and hands it to the speaker at the right time.
//
// The speaker PULLS (Fill): it knows when the period it is building will
// reach the DAC, and only a pull at that moment can place a sample to the
// millisecond. The existing music plane is push-and-play-in-order, which is
// why Sendspin cannot simply feed it.
type player struct {
	mu         sync.Mutex
	q          []chunk
	off        int // samples consumed from q[0]
	playing    bool
	arrived    chan struct{} // coalesced notification; the pull never holds mu while waiting
	generation uint64        // changes when a stream is cleared

	filter  *timeFilter
	delayUs int64 // static_delay_ms, µs

	gainTarget, gain int32 // Q15

	st playerStats

	arr arrivalLog

	// leadEwmaUs is a running average of how long before its play time a
	// chunk arrives, and shallow is whether that is short enough for the
	// speaker to run its ring shallow. Written under mu, read by the
	// speaker's goroutine without it.
	leadEwmaUs atomic.Int64
	shallow    atomic.Bool
}

const (
	// shallowBelowUs and shallowAboveUs are the lead at which a stream is
	// treated as low-latency, with hysteresis. Music Assistant asks for 2s and
	// a ring kept shallow for it would only add underflow risk.
	shallowBelowUs = 300_000
	shallowAboveUs = 400_000
)

// arrivalLog summarises how long before its play time each chunk arrived,
// once a minute. A chunk that arrives with little lead left is the part of a
// dropout the Echo cannot fix by itself. It counts into 1ms buckets: it runs
// under the player's lock, which the speaker's pull needs, and sorting a
// minute of leads there stalled the speaker once a minute (2026-10-24: a
// kernel DL1 underflow every 60s, to the second).
type arrivalLog struct {
	n        int
	min, max int64
	buckets  [arrivalBuckets]int32
	since    time.Time
}

const (
	arrivalBuckets = 1000
	arrivalFloorMs = -250 // bucket 0; leads outside the range clamp to the ends
)

func (a *arrivalLog) note(now time.Time, leadUs int64) {
	if a.since.IsZero() {
		a.since = now
	}
	if a.n == 0 || leadUs < a.min {
		a.min = leadUs
	}
	if a.n == 0 || leadUs > a.max {
		a.max = leadUs
	}
	a.n++
	i := int(leadUs/1000) - arrivalFloorMs
	a.buckets[min(max(i, 0), arrivalBuckets-1)]++
	if now.Sub(a.since) < time.Minute {
		return
	}
	log.Printf("[sendspin] arrival: n=%d lead min=%dms median=%dms max=%dms",
		a.n, a.min/1000, a.medianMs(), a.max/1000)
	*a = arrivalLog{since: now}
}

func (a *arrivalLog) medianMs() int {
	seen := 0
	for i, c := range a.buckets {
		if seen += int(c); seen*2 >= a.n {
			return i + arrivalFloorMs
		}
	}
	return 0
}

// shallowNext is the next value of the low-latency flag for a lead average.
func shallowNext(cur bool, ewmaUs int64) bool {
	switch {
	case ewmaUs < shallowBelowUs:
		return true
	case ewmaUs > shallowAboveUs:
		return false
	}
	return cur
}

// playerStats are counts since the stream started, for the status report.
type playerStats struct {
	Snaps         int   `json:"snaps"`
	Corrections   int   `json:"corrections"`
	Underruns     int   `json:"underruns"`
	LateDrops     int   `json:"lateDrops"`
	LastErrorUs   int   `json:"lastErrorUs"`
	Waits         int   `json:"waits"`
	WaitRecovered int   `json:"waitRecovered"`
	WaitTimeouts  int   `json:"waitTimeouts"`
	WaitMaxUs     int64 `json:"waitMaxUs"`
}

func newPlayer(f *timeFilter) *player {
	return &player{filter: f, gainTarget: unity, gain: unity, arrived: make(chan struct{}, 1)}
}

const unity int32 = 1 << 15

// volumeGain is the spec's perceptual curve: amplitude = (v/100)^1.5.
func volumeGain(volume int, muted bool) int32 {
	if muted || volume <= 0 {
		return 0
	}
	if volume >= 100 {
		return unity
	}
	return int32(math.Pow(float64(volume)/100, 1.5)*float64(unity) + 0.5)
}

func (p *player) setGain(volume int, muted bool) {
	p.mu.Lock()
	p.gainTarget = volumeGain(volume, muted)
	p.mu.Unlock()
}

func (p *player) setFilter(f *timeFilter) {
	p.mu.Lock()
	p.filter = f
	p.mu.Unlock()
}

func (p *player) setDelay(ms int) {
	p.mu.Lock()
	p.delayUs = int64(ms) * 1000
	p.mu.Unlock()
}

// push queues one decoded chunk. A chunk that starts before the one already
// at the tail is a server that went backwards; it is taken as a restart.
func (p *player) push(ts int64, pcm []int16) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if n := len(p.q); n > 0 && ts < p.q[n-1].ts {
		p.clearLocked()
	}
	if p.filter.synchronized() {
		lead := p.filter.clientTime(ts) - p.delayUs - nowUs()
		p.arr.note(time.Now(), lead)
		avg := p.leadEwmaUs.Load()
		if avg == 0 {
			avg = lead
		} else {
			avg += (lead - avg) / 64
		}
		p.leadEwmaUs.Store(avg)
		p.shallow.Store(shallowNext(p.shallow.Load(), avg))
	}
	p.q = append(p.q, chunk{ts: ts, pcm: pcm})
	p.notifyLocked()
}

// lowLatency reports a stream whose chunks arrive with little lead, which is
// what lets the speaker keep its ring shallow for it. Keep this policy through
// an empty queue: filling the hardware ring with silence during a packet gap
// makes recovery harder. clearLocked resets it when the stream actually ends.
func (p *player) lowLatency() bool { return p.shallow.Load() }

// clear drops everything buffered: stream/clear, stream/end, or HA taking
// the music plane.
func (p *player) clear() {
	p.mu.Lock()
	p.clearLocked()
	p.mu.Unlock()
}

func (p *player) clearLocked() {
	p.q, p.off, p.playing = nil, 0, false
	p.generation++
	p.notifyLocked()
	p.leadEwmaUs.Store(0)
	p.shallow.Store(false)
}

func (p *player) resetStats() {
	p.mu.Lock()
	p.st = playerStats{}
	p.mu.Unlock()
}

func (p *player) stats() playerStats {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.st
}

// buffered is how much audio is queued, as time.
func (p *player) buffered() time.Duration {
	p.mu.Lock()
	defer p.mu.Unlock()
	n := -p.off
	for _, c := range p.q {
		n += len(c.pcm)
	}
	return time.Duration(n) * time.Second / outRate
}

func (p *player) active() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.q) > 0
}

// Fill writes one period of stereo S16LE to out, whose first frame will
// reach the DAC at playAt. It returns false when there is nothing to play at
// that moment, so the speaker treats the music plane as empty.
func (p *player) Fill(out []byte, playAt time.Time) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	frames := len(out) / 4
	if len(p.q) == 0 || !p.filter.synchronized() {
		if p.playing {
			p.st.Underruns++
			p.playing = false
		}
		return false
	}

	plan := p.planLocked(frames, playAt)
	p.st.LastErrorUs = int(plan.errUs)
	lead, repeat := plan.lead, plan.repeat
	if plan.notDue {
		p.playing = false
		return false
	}
	if plan.drop > 0 && !p.dropLocked(plan.drop) {
		p.playing = false
		return false
	}
	if plan.snap {
		p.playing = true
		p.st.Snaps++
	} else if plan.drop > 0 || repeat > 0 {
		p.st.Corrections++
	}

	start, end := p.gain, p.gainTarget
	p.gain = end
	gainAt := func(i int) int32 {
		if start == end {
			return end
		}
		return start + (end-start)*int32(i)/int32(frames)
	}

	i := 0
	for ; i < lead; i++ {
		put(out, i, 0)
	}
	if repeat > 0 {
		s := p.q[0].pcm[p.off]
		for r := 0; r < repeat && i < frames; r++ {
			put(out, i, scale(s, gainAt(i)))
			i++
		}
	}
	for i < frames {
		if len(p.q) == 0 {
			// Ran dry mid-period: the rest is silence, and the next Fill
			// resyncs from scratch.
			for ; i < frames; i++ {
				put(out, i, 0)
			}
			p.playing = false
			p.st.Underruns++
			break
		}
		c := p.q[0].pcm
		n := min(len(c)-p.off, frames-i)
		for k := 0; k < n; k++ {
			put(out, i+k, scale(c[p.off+k], gainAt(i+k)))
		}
		i += n
		p.advanceLocked(n)
	}
	return true
}

// localTimeLocked is when the next sample should reach the DAC on our clock.
func (p *player) localTimeLocked() int64 {
	c := p.q[0]
	ts := c.ts + int64(p.off)*1e6/outRate
	return p.filter.clientTime(ts) - p.delayUs
}

// dropLocked discards n samples, reporting false if the queue ran out first.
func (p *player) dropLocked(n int) bool {
	for n > 0 && len(p.q) > 0 {
		k := min(len(p.q[0].pcm)-p.off, n)
		if k == len(p.q[0].pcm)-p.off && p.off == 0 {
			p.st.LateDrops++
		}
		p.advanceLocked(k)
		n -= k
	}
	return len(p.q) > 0
}

func (p *player) advanceLocked(n int) {
	p.off += n
	for len(p.q) > 0 && p.off >= len(p.q[0].pcm) {
		p.off -= len(p.q[0].pcm)
		p.q[0].pcm = nil
		p.q = p.q[1:]
	}
}

func scale(s int16, g int32) int16 {
	if g == unity {
		return s
	}
	return int16((int32(s) * g) >> 15)
}

// put writes one mono sample to both channels of stereo frame i.
func put(out []byte, i int, s int16) {
	u := uint16(s)
	out[i*4], out[i*4+1] = byte(u), byte(u>>8)
	out[i*4+2], out[i*4+3] = byte(u), byte(u>>8)
}
