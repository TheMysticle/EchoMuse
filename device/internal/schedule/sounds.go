package schedule

import (
	"encoding/binary"
	"errors"
	"sync"

	"github.com/wilbowes/EchoMuse/internal/earcon"
)

// Amazon's EarconsAudioProvider::alarmDefault() selects melodic_01; timerDefault()
// selects melodic_02. Read the existing system file rather than redistribute it.
const StockAlarmPath = "/system/local/share/earcon/base/system_alerts_melodic_01.mp3"

// reminderDefault() in the stock libEarconAdapter selects notification_03.
const StockReminderPath = "/system/local/share/earcon/base/alerts_notification_03.mp3"

var stockReminder = sync.OnceValue(func() []byte { return loadAlarm(StockReminderPath) })

var stockAlarm = sync.OnceValue(func() []byte { return loadAlarm(StockAlarmPath) })

func loadAlarm(path string) []byte {
	samples, err := earcon.Load(path)
	if err != nil || len(samples) == 0 {
		return TimerAlarmSound
	}
	pcm := make([]byte, len(samples)*2)
	for i, sample := range samples {
		binary.LittleEndian.PutUint16(pcm[i*2:], uint16(int16(sample)))
	}
	return pcm
}

// CheckSounds runs before acknowledging settings, so an uncached or corrupt
// upload cannot replace a working selection.
func CheckSounds(dir string, settings Settings) error {
	for _, id := range settings.Sounds {
		if id != "" {
			if _, err := ReadAsset(dir, id); err != nil {
				return errors.New("sound asset is not cached on device")
			}
		}
	}
	return nil
}

// Sound applies the satellite-wide selection to every existing and future
// alert of this kind. Legacy per-item sound metadata is no longer consulted.
// Missing/corrupt custom files fall back to the same default as an empty choice.
func Sound(dir, kind string, settings Settings) []byte {
	if id := settings.Sounds[kind]; id != "" {
		if pcm, err := ReadAsset(dir, id); err == nil && len(pcm) > 0 {
			return pcm
		}
	}
	switch kind {
	case "timer":
		return TimerAlarmSound
	case "alarm":
		return stockAlarm()
	case "reminder":
		return stockReminder()
	default:
		return nil
	}
}
