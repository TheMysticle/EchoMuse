package server

import (
	"log"
	"math"
	"time"

	"github.com/wilbowes/EchoMuse/pkg/led"
)

// flashState is the ring's one-shot acknowledgement. While a flash is up,
// SetLEDs and SetDirectionLEDs record their frames but do not paint, so the
// link pulse (every 50ms) and the animator cannot cut it short.
type flashState struct {
	gen    uint64
	active bool
}

func (s *Server) flashActive() bool {
	s.flashMu.Lock()
	defer s.flashMu.Unlock()
	return s.flash.active
}

// Flash paints the whole ring one colour for d, then hands it back to
// whatever owns it: the mute ring, the volume arc, or the latest frame.
// It acknowledges a gesture that otherwise shows nothing, such as a pairing
// hold on a connected device.
func (s *Server) Flash(r, g, b uint8, d time.Duration) {
	s.ledMu.Lock()
	lc := s.ledController
	s.ledMu.Unlock()
	if lc == nil {
		return
	}

	s.flashMu.Lock()
	s.flash.gen++
	s.flash.active = true
	gen := s.flash.gen
	s.flashMu.Unlock()

	leds := make([]led.Led, 12)
	for i := range leds {
		leds[i] = led.Led{ID: i, R: r, G: g, B: b}
	}
	if err := lc.SetLEDs(leds...); err != nil {
		log.Printf("Flash LED set failed: %v", err)
	}

	time.AfterFunc(d, func() {
		s.flashMu.Lock()
		if s.flash.gen != gen {
			s.flashMu.Unlock()
			return // a newer flash owns the ring
		}
		s.flash.active = false
		s.flashMu.Unlock()
		s.restoreRing()
	})
}

// FlashPulse pulses the whole ring in color (r, g, b) with a sinusoidal throb
// for duration d, then hands the ring back to whatever owns it (mute or base LEDs).
func (s *Server) FlashPulse(r, g, b uint8, d time.Duration) {
	s.ledMu.Lock()
	lc := s.ledController
	s.ledMu.Unlock()
	if lc == nil {
		return
	}

	s.flashMu.Lock()
	s.flash.gen++
	s.flash.active = true
	gen := s.flash.gen
	s.flashMu.Unlock()

	go func() {
		const (
			minBr  = 0.08
			maxBr  = 1.0
			period = 1500 * time.Millisecond
			stepMs = 50
		)
		start := time.Now()
		deadline := start.Add(d)

		paint := func(now time.Time) {
			t := math.Mod(float64(now.Sub(start))/float64(period), 1.0)
			br := minBr + (maxBr-minBr)*(0.5+0.5*math.Sin(2*math.Pi*t))
			leds := make([]led.Led, 12)
			for i := range leds {
				leds[i] = led.Led{
					ID: i,
					R:  uint8(float64(r) * br),
					G:  uint8(float64(g) * br),
					B:  uint8(float64(b) * br),
				}
			}
			_ = lc.SetLEDs(leds...)
		}

		// Paint initial frame immediately for zero response latency
		paint(start)

		ticker := time.NewTicker(stepMs * time.Millisecond)
		defer ticker.Stop()

		for {
			now := <-ticker.C
			s.flashMu.Lock()
			current := s.flash.active && s.flash.gen == gen
			s.flashMu.Unlock()
			if !current || now.After(deadline) {
				break
			}
			paint(now)
		}

		s.flashMu.Lock()
		owned := s.flash.gen == gen
		if owned {
			s.flash.active = false
		}
		s.flashMu.Unlock()
		// A newer flash owns the ring; repainting here would glitch it.
		if owned {
			s.restoreRing()
		}
	}()
}

// restoreRing repaints the ring from state after something held it.
func (s *Server) restoreRing() {
	if s.volume != nil && s.volume.DisplayActive() {
		return // the arc repaints the base itself when it expires
	}
	pulseActive := s.LinkDown() && !s.PulseSuppressed()
	if s.mute != nil && s.mute.IsMuted() && !pulseActive {
		s.mute.showMuteLEDs()
		return
	}
	s.paintBaseLEDs()
}
