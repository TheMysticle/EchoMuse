package main

// Runs the firmware's endpointer on a recorded session.
//
// A session recording starts after the wake word, so it has no room before it.
// The endpointer's reference is the level of the seconds before the wake word,
// so for a recording whose later part is the TV alone (the TV runs on after the
// talker stops) that part stands in for the history: it is played first, then
// the recording, with the session opened between them.

import (
	"encoding/binary"
	"fmt"
	"os"

	"github.com/wilbowes/EchoMuse/internal/beamformer"
)

// readWAV16 reads a 16kHz mono 16-bit WAV's samples as S16LE bytes.
func readWAV16(path string) ([]byte, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	for i := 12; i+8 <= len(b); {
		id, n := string(b[i:i+4]), int(binary.LittleEndian.Uint32(b[i+4:]))
		if id == "data" {
			end := min(i+8+n, len(b))
			return b[i+8 : end], nil
		}
		i += 8 + n + n%2
	}
	return nil, fmt.Errorf("%s: no data chunk", path)
}

// epClipReport prints when the endpointer ends the recorded session at path.
func epClipReport(path string, historyS float64) {
	pcm, err := readWAV16(path)
	if err != nil {
		fmt.Println(err)
		return
	}
	// The room before the wake word: the recording after the first 2.5s (the
	// command's own start), repeated until there are historyS seconds of it.
	skip := min(int(2.5*16000)*2, len(pcm))
	tv := pcm[skip:]
	var hist []byte
	for len(hist) < int(historyS*16000*2) && len(tv) > 0 {
		hist = append(hist, tv...)
	}
	ep := beamformer.NewEndpointer()
	feed := func(b []byte, from int) (endedMs int) {
		endedMs = -1
		for i := 0; i+2560 <= len(b); i += 2560 {
			ep.Push(b[i : i+2560])
			if ep.Ended() && endedMs < 0 {
				endedMs = from + (i+2560)/32
			}
		}
		return
	}
	feed(hist, 0) // the TV alone, as the room before the wake word
	ep.StartSession(0)
	end := feed(pcm, 0)
	state := "no end sent: the controller's own ending stands"
	if end >= 0 {
		state = fmt.Sprintf("end of speech sent %.1fs into the session", float64(end)/1000)
	}
	ratio, ref, _ := ep.State()
	fmt.Printf("%-32s %5.1fs recorded, warm=%v, %s (last level %.1fx the reference %.0f)\n",
		path[len(path)-24:], float64(len(pcm))/32000, ep.Warm(), state, ratio, ref)
}
