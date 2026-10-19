package server

import (
	"os"
	"path/filepath"
	"sync"
	"testing"
)

func TestDeviceStateRoundtrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "etc", "state.json")

	// Missing file → not ok.
	if _, ok := loadDeviceState(path); ok {
		t.Fatal("expected ok=false for missing file")
	}

	// Save creates parent dirs and persists the muted flag.
	saveDeviceState(path, deviceState{Muted: true})
	st, ok := loadDeviceState(path)
	if !ok || !st.Muted {
		t.Fatalf("roundtrip failed: ok=%v st=%+v", ok, st)
	}

	saveDeviceState(path, deviceState{Muted: false})
	st, ok = loadDeviceState(path)
	if !ok || st.Muted {
		t.Fatalf("overwrite failed: ok=%v st=%+v", ok, st)
	}

	// Corrupt file → not ok, no panic.
	if err := os.WriteFile(path, []byte("{truncated"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, ok := loadDeviceState(path); ok {
		t.Fatal("expected ok=false for corrupt file")
	}
}

func TestSpotifyStateRoundtrip(t *testing.T) {
	orig := statePath
	statePath = filepath.Join(t.TempDir(), "etc", "state.json")
	t.Cleanup(func() { statePath = orig })

	// When no state exists, LoadSpotifyState returns false.
	if _, ok := LoadSpotifyState(); ok {
		t.Fatal("expected ok=false when no state exists")
	}

	// Persisting Spotify state saves all fields.
	SaveSpotifyState(true, true, "Living Room")
	sp, ok := LoadSpotifyState()
	if !ok || !sp.Enabled || !sp.Standalone || sp.Name != "Living Room" {
		t.Fatalf("unexpected spotify state: ok=%v %+v", ok, sp)
	}

	// Mute toggle preserves Spotify fields.
	saveDeviceState(statePath, deviceState{
		Muted:             true,
		SpotifyEnabled:    sp.Enabled,
		SpotifyStandalone: sp.Standalone,
		SpotifyName:       sp.Name,
	})
	sp, ok = LoadSpotifyState()
	if !ok || !sp.Enabled || !sp.Standalone || sp.Name != "Living Room" {
		t.Fatalf("spotify state lost on mute: ok=%v %+v", ok, sp)
	}
	st, ok := loadDeviceState(statePath)
	if !ok || !st.Muted {
		t.Fatalf("muted state lost: ok=%v %+v", ok, st)
	}

	// Disabling standalone causes LoadSpotifyState to return ok=false.
	SaveSpotifyState(true, false, "Living Room")
	if _, ok := LoadSpotifyState(); ok {
		t.Fatal("expected ok=false when SpotifyStandalone is false")
	}
	// But mute state is still preserved.
	st, ok = loadDeviceState(statePath)
	if !ok || !st.Muted {
		t.Fatalf("muted state lost after SaveSpotifyState: ok=%v %+v", ok, st)
	}
}

func TestSaveMutedAndWakeWordPreserveEachOther(t *testing.T) {
	orig := statePath
	statePath = filepath.Join(t.TempDir(), "etc", "state.json")
	t.Cleanup(func() { statePath = orig })

	SaveSpotifyState(true, true, "Kitchen")
	SaveWakeWordState("on", "hey_test")
	saveMuted(true)

	st, ok := loadDeviceState(statePath)
	if !ok || !st.Muted || !st.SpotifyStandalone || st.SpotifyName != "Kitchen" ||
		st.OwwOnDevice != "on" || st.OwwModel != "hey_test" {
		t.Fatalf("fields clobbered: ok=%v %+v", ok, st)
	}
}

func TestStateUpdatesAreSerialised(t *testing.T) {
	orig := statePath
	statePath = filepath.Join(t.TempDir(), "etc", "state.json")
	t.Cleanup(func() { statePath = orig })

	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(3)
		go func(i int) { defer wg.Done(); saveMuted(i%2 == 0) }(i)
		go func(i int) { defer wg.Done(); SaveSpotifyState(true, true, "Den") }(i)
		go func(i int) { defer wg.Done(); SaveWakeWordState("on", "m") }(i)
	}
	wg.Wait()
	st, ok := loadDeviceState(statePath)
	if !ok || !st.SpotifyStandalone || st.SpotifyName != "Den" || st.OwwModel != "m" {
		t.Fatalf("lost update: ok=%v %+v", ok, st)
	}
}

// An angle near the edge between two LEDs must not flip the marker between
// them, and a real move must still get there.
func TestRingSectorHasHysteresis(t *testing.T) {
	if got := ringSector(100, 0, false); got != 90 {
		t.Errorf("fresh 100 = %d, want 90", got)
	}
	if got := ringSector(359, 0, false); got != 0 {
		t.Errorf("359 = %d, want 0 (wraps)", got)
	}
	// The edge between sectors 90 and 120 is 105; the marker stays on 90 until
	// the angle is past 120 (edge + the hysteresis).
	if got := ringSector(118, 90, true); got != 90 {
		t.Errorf("118 from 90 moved to %d, want to stay", got)
	}
	if got := ringSector(122, 90, true); got != 120 {
		t.Errorf("122 from 90 = %d, want 120", got)
	}
	// Across north, in both directions.
	if got := ringSector(329, 0, true); got != 330 {
		t.Errorf("329 from 0 = %d, want 330", got)
	}
	if got := ringSector(1, 330, true); got != 0 {
		t.Errorf("1 from 330 = %d, want 0", got)
	}
	if got := ringSector(340, 0, true); got != 0 {
		t.Errorf("340 from 0 = %d, want to stay on 0", got)
	}
}
