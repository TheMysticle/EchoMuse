package speaker

import (
	"log"

	"github.com/wilbowes/EchoMuse/internal/outchain"
)

// stockTuning reads the board's stock playback tuning from the device's own
// /system (board.Speaker.StockTuning), for the output chain. Nil when the
// board names none, or its files are missing or unreadable: the chain then
// plays the board's voicing.
func stockTuning(dir string) *outchain.RadarTuning {
	if dir == "" {
		return nil
	}
	rt, err := outchain.LoadRadarTuning(dir)
	if err != nil {
		log.Printf("[speaker] no stock tuning from %s: %v", dir, err)
		return nil
	}
	log.Printf("[speaker] stock tuning from %s: %s", dir, rt)
	return rt
}
