package schedule

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestGlobalSoundsApplyToExistingAndFutureAlertsAndSurviveRestart(t *testing.T) {
	for _, kind := range []string{"timer", "alarm", "reminder"} {
		t.Run(kind, func(t *testing.T) {
			e, now, _ := fixture(t)
			dir := t.TempDir()
			pcm := []byte{25, 0, 40, 0}
			id := assetID(pcm)
			if err := PutAsset(dir, id, pcm); err != nil {
				t.Fatal(err)
			}
			i := timer("existing", 5)
			i.Kind = kind
			i.Sound = strings.Repeat("b", 64) // Legacy per-item metadata must not override the global choice.
			apply(t, e, "create", "", i, 0)
			settings := e.Snapshot().Settings
			settings.ReminderRepeat = true
			settings.ReminderRepeats = 2
			settings.Sounds[kind] = id
			if err := CheckSounds(dir, settings); err != nil {
				t.Fatal(err)
			}
			if _, err := e.Apply(Command{Owner: "dot-A", Request: "global", Action: "settings", Settings: &settings}); err != nil {
				t.Fatal(err)
			}
			future := timer("future", 10)
			future.Kind = kind
			apply(t, e, "create", "", future, 0)
			restored, err := Open(e.path, "dot-A", e.now, e.valid)
			if err != nil {
				t.Fatal(err)
			}
			for _, name := range []string{"existing", "future"} {
				*now = now.Add(5 * time.Second)
				restored.Tick()
				item, got, ok := restored.Active()
				if !got.ReminderRepeat || got.ReminderRepeats != 2 {
					t.Fatal("reminder settings lost after restart")
				}
				if !ok || item.ID != name || !bytes.Equal(Sound(dir, item.Kind, got), pcm) {
					t.Fatalf("wrong playback for %s: %+v", name, item)
				}
				apply(t, restored, "dismiss", name, nil, 0)
			}
		})
	}
}

func TestGlobalSoundsValidationDefaultsAndPruning(t *testing.T) {
	dir := t.TempDir()
	settings := DefaultSettings()
	for n, kind := range []string{"timer", "alarm", "reminder"} {
		pcm := []byte{byte(n + 1), 0}
		id := assetID(pcm)
		if err := PutAsset(dir, id, pcm); err != nil {
			t.Fatal(err)
		}
		settings.Sounds[kind] = id
	}
	if n, err := PruneAssets(dir, nil, settings); err != nil || n != 0 {
		t.Fatal(n, err)
	}
	if err := CheckSounds(dir, settings); err != nil {
		t.Fatal(err)
	}
	for _, kind := range []string{"timer", "alarm", "reminder"} {
		selected := Sound(dir, kind, settings)
		if len(selected) != 2 {
			t.Fatalf("custom %s not selected", kind)
		}
		// A corrupt cache must produce the kind's default rather than silence.
		if err := os.WriteFile(filepath.Join(dir, settings.Sounds[kind]), []byte{0, 0}, 0600); err != nil {
			t.Fatal(err)
		}
		if err := CheckSounds(dir, settings); err == nil {
			t.Fatal("corrupt cache accepted")
		}
		if !bytes.Equal(Sound(dir, kind, settings), Sound(dir, kind, DefaultSettings())) {
			t.Fatal("wrong fallback")
		}
		settings.Sounds[kind] = ""
	}
	if n, err := PruneAssets(dir, nil, settings); err != nil || n != 3 {
		t.Fatal(n, err)
	}
	if !bytes.Equal(Sound(dir, "timer", settings), TimerAlarmSound) || len(Sound(dir, "reminder", settings)) == 0 {
		t.Fatal("default alert sound is missing")
	}
	settings.Sounds["alarm"] = strings.Repeat("a", 64)
	if err := CheckSounds(dir, settings); err == nil {
		t.Fatal("missing cache accepted")
	}
	for _, sounds := range []map[string]string{{"alarm": "../bad"}, {"music": ""}} {
		settings.Sounds = sounds
		if validateSettings(settings) == nil {
			t.Fatal("invalid sound settings accepted")
		}
	}
}

func TestOlderSettingsPreserveSoundsAndExplicitDefaultClearsThem(t *testing.T) {
	e, _, _ := fixture(t)
	settings := e.Snapshot().Settings
	settings.Sounds["timer"] = strings.Repeat("a", 64)
	for n, sounds := range []map[string]string{settings.Sounds, nil, {}} {
		settings.Sounds = sounds
		if _, err := e.Apply(Command{Owner: "dot-A", Request: []string{"custom", "old-client", "default"}[n], Action: "settings", Settings: &settings}); err != nil {
			t.Fatal(err)
		}
		got := e.Snapshot().Settings.Sounds["timer"]
		if (got != "") != (n < 2) {
			t.Fatalf("unexpected selection after step %d: %q", n, got)
		}
	}
	legacy := e.Snapshot().State
	legacy.Settings.Sounds = nil
	data, err := encode(legacy)
	if err != nil {
		t.Fatal(err)
	}
	restored, err := decode(data, "dot-A")
	if err != nil || restored.Settings.Sounds == nil {
		t.Fatal("older snapshot failed migration", err)
	}
}

func TestStockAlarmDecodeAndMissingFileFallback(t *testing.T) {
	if !bytes.Equal(loadAlarm(filepath.Join(t.TempDir(), "missing.mp3")), TimerAlarmSound) {
		t.Fatal("missing stock sound has no fallback")
	}
	path := os.Getenv("ECHOMUSE_TEST_STOCK_ALARM")
	if path == "" {
		t.Skip("set ECHOMUSE_TEST_STOCK_ALARM to validate the firmware dump")
	}
	pcm := loadAlarm(path)
	if len(pcm) < 48000*2 || len(pcm)%2 != 0 || bytes.Equal(pcm, TimerAlarmSound) {
		t.Fatal("stock alarm did not decode to distinct mono PCM")
	}
	if bytes.Equal(pcm, make([]byte, len(pcm))) {
		t.Fatal("stock alarm is silent")
	}
	t.Logf("stock alarm: %d samples at 48 kHz", len(pcm)/2)
}

func TestReminderRepeatBounds(t *testing.T) {
	for _, count := range []int{-1, 11} {
		s := DefaultSettings()
		s.ReminderRepeat, s.ReminderRepeats = true, count
		if validateSettings(s) == nil {
			t.Fatal("unbounded repetition accepted")
		}
	}
}
