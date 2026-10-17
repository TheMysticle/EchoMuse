package server

import (
	"encoding/json"
	"log"
	"os"
	"path/filepath"
	"sync"
)

// statePath persists mute state across reboots and OTA restarts. It lives
// next to the TLS credentials in /data/local/etc — OTA slot flips only touch
// /data/local/bin, so the file survives them.
//
// Only mute lives here: mute is device-sovereign (the physical button cannot
// be overridden remotely, so the device must restore it itself, controller or
// no controller). Volume is the opposite — the controller's stored
// startupVolume is the source of truth, re-applied via SeedVolume on the
// first config push each run.
var statePath = "/data/local/etc/echomuse/state.json"

type deviceState struct {
	Muted bool `json:"muted"`
	// Spotify fields are only written when the controller has pushed a
	// Spotify config. They let the Connect engine restart after a reboot
	// with no controller yet, when SpotifyStandalone was on.
	SpotifyEnabled    bool   `json:"spotifyEnabled,omitempty"`
	SpotifyStandalone bool   `json:"spotifyStandalone,omitempty"`
	SpotifyName       string `json:"spotifyName,omitempty"`
	OwwOnDevice       string `json:"owwOnDevice,omitempty"`
	OwwModel          string `json:"owwModel,omitempty"`
}

// loadDeviceState reads the persisted state. ok=false means no usable state
// (first boot of persisting firmware, or unreadable file) — boot unmuted as
// before.
func loadDeviceState(path string) (st deviceState, ok bool) {
	data, err := os.ReadFile(path)
	if err != nil {
		return deviceState{}, false
	}
	if err := json.Unmarshal(data, &st); err != nil {
		log.Printf("[state] %s unreadable, ignoring: %v", path, err)
		return deviceState{}, false
	}
	return st, true
}

// saveDeviceState writes the state atomically (write-then-rename, so a power
// cut mid-write can't leave a truncated file). Called on every mute toggle —
// rare enough that a synchronous flash write needs no debounce.
func saveDeviceState(path string, st deviceState) {
	data, err := json.Marshal(st)
	if err != nil {
		log.Printf("[state] marshal failed: %v", err)
		return
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		log.Printf("[state] mkdir failed: %v", err)
		return
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		log.Printf("[state] write failed: %v", err)
		return
	}
	if err := os.Rename(tmp, path); err != nil {
		log.Printf("[state] rename failed: %v", err)
	}
}

// stateMu serialises every read-modify-write of the state file. The mute
// button, config pushes (Spotify + wake word) and boot restore all update
// different fields of the same JSON file from different goroutines; without
// the lock one writer's load/save could overwrite another's change, or two
// writers could clobber the shared .tmp file.
var stateMu sync.Mutex

// updateDeviceState loads the state, applies fn and saves it, atomically with
// respect to other updaters. The file is only rewritten when fn changed
// something, which keeps repeated config pushes from wearing flash.
func updateDeviceState(fn func(st *deviceState)) {
	stateMu.Lock()
	defer stateMu.Unlock()
	st, _ := loadDeviceState(statePath)
	before := st
	fn(&st)
	if st == before {
		return
	}
	saveDeviceState(statePath, st)
}

// SpotifyState is the subset of device state the Spotify engine needs at boot.
type SpotifyState struct {
	Enabled     bool
	Standalone  bool
	Name        string
	OwwOnDevice string
	OwwModel    string
}

// LoadSpotifyState returns the last controller-pushed Spotify settings that
// were persisted for controllerless operation. ok is false when nothing was
// saved (first boot, or standalone never turned on).
func LoadSpotifyState() (SpotifyState, bool) {
	stateMu.Lock()
	defer stateMu.Unlock()
	st, ok := loadDeviceState(statePath)
	if !ok || !st.SpotifyStandalone {
		return SpotifyState{}, false
	}
	return SpotifyState{
		Enabled:     st.SpotifyEnabled,
		Standalone:  st.SpotifyStandalone,
		Name:        st.SpotifyName,
		OwwOnDevice: st.OwwOnDevice,
		OwwModel:    st.OwwModel,
	}, true
}

// SaveSpotifyState records the Spotify flags so a reboot without a controller
// can restart the engine when standalone is on. Preserves all other fields.
func SaveSpotifyState(enabled, standalone bool, name string) {
	updateDeviceState(func(st *deviceState) {
		st.SpotifyEnabled = enabled
		st.SpotifyStandalone = standalone
		st.SpotifyName = name
	})
}

// SaveWakeWordState records the on-device wake-word engine settings so a
// controllerless reboot can keep listening for the wake word.
func SaveWakeWordState(owwOnDevice, owwModel string) {
	updateDeviceState(func(st *deviceState) {
		st.OwwOnDevice = owwOnDevice
		st.OwwModel = owwModel
	})
}

// saveMuted records the mute flag, preserving every other field.
func saveMuted(muted bool) {
	updateDeviceState(func(st *deviceState) { st.Muted = muted })
}
