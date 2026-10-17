package server

import (
	"sync"

	"github.com/wilbowes/EchoMuse/pkg/led"
)

type alertRing struct {
	mu     sync.Mutex
	gen    int
	active bool
	color  [3]uint8
}

// SetAlertRing uses the existing pulse renderer on its own generation. Link
// loss and controller animations can neither stop it nor own its LED frames.
func (s *Server) SetAlertRing(color *[3]uint8) {
	s.alert.mu.Lock()
	s.alert.gen++
	gen := s.alert.gen
	s.alert.active = color != nil
	if color != nil {
		s.alert.color = *color
	}
	s.alert.mu.Unlock()
	if color == nil {
		s.restoreRing()
		return
	}
	s.CancelVolumeDisplay()
	go s.runPulse(gen, AnimSpec{Pattern: "pulse", Colors: [][3]uint8{*color}, PeriodMs: 2000, Alert: true})
}
func (s *Server) alertActive() bool {
	s.alert.mu.Lock()
	defer s.alert.mu.Unlock()
	return s.alert.active
}
func (s *Server) pulseCurrent(gen int, alert bool) bool {
	if !alert {
		return s.animCurrent(gen)
	}
	s.alert.mu.Lock()
	defer s.alert.mu.Unlock()
	return s.alert.active && s.alert.gen == gen
}
func (s *Server) paintAlert(gen int, frame []led.Led) {
	if s.voiceOwnsRing() {
		return
	}
	s.alert.mu.Lock()
	defer s.alert.mu.Unlock()
	if !s.alert.active || s.alert.gen != gen {
		return
	}
	s.ledMu.Lock()
	defer s.ledMu.Unlock()
	if s.ledController != nil {
		_ = s.ledController.SetLEDs(frame...)
	}
}
