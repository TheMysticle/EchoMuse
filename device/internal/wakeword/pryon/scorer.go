package pryon

import (
	"fmt"
	"math"
	"sync"
	"sync/atomic"
	"time"

	"github.com/wilbowes/EchoMuse/internal/wakeword/shadow"
)

// The rules of package shadow apply here unchanged, because the same goroutine
// calls us: PushBytesAt runs on the mic loop, which reads 160ms ALSA batches out
// of a ring only 160ms deep, so it hands the samples to a buffered channel and
// returns. A separate goroutine does the (still short) push into the decoder,
// and a frame that cannot be taken is DROPPED AND COUNTED, never waited for.
const queueFrames = 8

// maxBacklogMs is how much unscored audio the decoder may hold before we stop
// feeding it. It drops audio itself when its backlog grows, silently, and a
// silent loss is the worst kind of wrong answer here, so we drop first and say
// so in the counters.
const maxBacklogMs = 600

// DefaultRefractory matches the open engine: one utterance, one wake.
const DefaultRefractory = 1500 * time.Millisecond

// backend is the decoder. The real one is cBackend; tests inject a fake, which
// is why none of the logic below needs the library.
type backend interface {
	push(ts uint64, pcm []int16) error
	backlogMs() int
	close()
}

type queued struct {
	pcm []int16
	at  time.Time
}

// Scorer is the Echo's own decoder as a shadow.Engine.
type Scorer struct {
	b       backend
	onCross func(score, threshold float32, at time.Time)
	info    string

	ch     chan queued
	quit   chan struct{}
	done   chan struct{}
	stop   sync.Once
	closed atomic.Bool

	times *timeRing

	// pos is samples pushed so far: the timestamp the decoder is given. It is
	// touched by the worker goroutine only.
	pos uint64

	maxGapMs atomic.Int64
	lastPush time.Time // producer only

	mu             sync.Mutex
	stats          shadow.Stats
	lastCross      time.Time
	refract        time.Duration
	bargeThreshold float32
	speakerActive  func() bool
	bargePeak      float32
	bargeFrames    uint64
	bargeBar       float32
}

func newScorer(b backend, onCross func(score, threshold float32, at time.Time)) *Scorer {
	s := &Scorer{
		b:       b,
		onCross: onCross,
		ch:      make(chan queued, queueFrames),
		quit:    make(chan struct{}),
		done:    make(chan struct{}),
		times:   newTimeRing(),
		refract: DefaultRefractory,
	}
	s.stats.Threshold = AcceptBar
	active.Store(s)
	go s.run()
	return s
}

// PushBytesAt queues one chunk of little-endian S16 16kHz mono. It never
// blocks. The samples are copied: the mic pipeline reuses its buffers.
func (s *Scorer) PushBytesAt(b []byte, at time.Time) {
	n := len(b) / 2
	if n == 0 || s.closed.Load() {
		return
	}
	if !s.lastPush.IsZero() {
		if gap := at.Sub(s.lastPush).Milliseconds(); gap > s.maxGapMs.Load() {
			s.maxGapMs.Store(gap)
		}
	}
	s.lastPush = at
	cp := make([]int16, n)
	for i := 0; i < n; i++ {
		cp[i] = int16(uint16(b[2*i]) | uint16(b[2*i+1])<<8)
	}
	select {
	case s.ch <- queued{cp, at}:
	default:
		s.mu.Lock()
		s.stats.Drops++
		s.mu.Unlock()
	}
}

func (s *Scorer) run() {
	defer close(s.done)
	for {
		select {
		case <-s.quit:
			return
		case q := <-s.ch:
			s.feed(q)
		}
	}
}

func (s *Scorer) feed(q queued) {
	if s.b.backlogMs() > maxBacklogMs {
		s.mu.Lock()
		s.stats.Drops++
		s.mu.Unlock()
		return
	}
	end := s.pos + uint64(len(q.pcm))
	// Stamped BEFORE the push: the decoder may call back from its own thread
	// before the push returns, and the callback looks this chunk up.
	s.times.add(end, q.at)
	err := s.b.push(s.pos, q.pcm)
	s.pos = end
	s.mu.Lock()
	s.stats.Frames++
	if err != nil {
		s.stats.Errors++
		s.stats.LastErr = err.Error()
	}
	s.mu.Unlock()
}

// onResult is called from the decoder's thread for every result it reports,
// accepts and near misses alike, for ALEXA and for the package's STOP.
func (s *Scorer) onResult(typ uint32, keyword string, begin, end uint64, score float32) {
	if s.closed.Load() || keyword != Keyword {
		return
	}
	now := time.Now()
	s.mu.Lock()
	playing := s.bargeThreshold > 0 && s.speakerActive != nil && s.speakerActive()
	crossed, bar := Judge(typ, score, playing)
	if score == 0 && typ == TypeAccept {
		score = bar // an accept whose score line was not seen still cleared the bar
	}
	if score > s.stats.MaxScore {
		s.stats.MaxScore = score
	}
	s.stats.Threshold = bar
	if playing {
		// What the barge window reports: every decision at the playback bar,
		// because the decoder says nothing about the audio it did not score.
		s.bargeFrames++
		s.bargeBar = bar
		if score > s.bargePeak {
			s.bargePeak = score
		}
	}
	if crossed && !s.lastCross.IsZero() && now.Sub(s.lastCross) < s.refract {
		crossed = false
	}
	if crossed {
		s.lastCross = now
		s.stats.Crossings++
	}
	cb := s.onCross
	s.mu.Unlock()
	if crossed && cb != nil {
		cb(score, bar, s.times.when(end, now))
	}
}

// Reset is a no-op: the decoder keeps one continuous timeline, and a wake word
// cannot span a stream boundary, so there is nothing a boundary needs cleared.
func (s *Scorer) Reset() {}

// Ready is true from the moment the decoder is built; there is no warm-up the
// way the open engine has one.
func (s *Scorer) Ready() bool { return !s.closed.Load() }

// Drain returns the counters since the last call and clears them.
func (s *Scorer) Drain() shadow.Stats {
	s.mu.Lock()
	defer s.mu.Unlock()
	st := s.stats
	st.MaxGapMs = uint32(s.maxGapMs.Swap(0))
	s.stats = shadow.Stats{Threshold: st.Threshold}
	return st
}

// TakeBargeWindow returns what was decided while audio played, and clears it.
func (s *Scorer) TakeBargeWindow() (peak, bar float32, frames uint64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	peak, bar, frames = s.bargePeak, s.bargeBar, s.bargeFrames
	s.bargePeak, s.bargeFrames = 0, 0
	if bar == 0 {
		bar = PlaybackBar
	}
	return
}

// SetThreshold is accepted and ignored: the model's own bars are what it was
// calibrated against.
func (s *Scorer) SetThreshold(float32) {}

// SetBargeThreshold turns the lower, playback bar on (any t > 0) with the
// predicate that says when audio is playing. The bar itself is the model's.
func (s *Scorer) SetBargeThreshold(t float32, active func() bool) {
	s.mu.Lock()
	s.bargeThreshold = t
	s.speakerActive = active
	s.mu.Unlock()
}

// Info describes the engine for the log.
func (s *Scorer) Info() string {
	if s.info != "" {
		return s.info
	}
	return fmt.Sprintf("pryon, accept %.3f", AcceptBar)
}

// Close stops the worker and releases the decoder and the model set. Safe to
// call while frames are still arriving, and more than once: the mic goroutine
// holds the scorer for a stream and keeps pushing to it, so nothing is closed
// that a sender could panic on.
func (s *Scorer) Close() {
	s.stop.Do(func() {
		s.closed.Store(true)
		active.CompareAndSwap(s, nil)
		close(s.quit)
		<-s.done
		s.b.close()
	})
}

func floatBits(f float32) uint32     { return math.Float32bits(f) }
func floatFromBits(b uint32) float32 { return math.Float32frombits(b) }
