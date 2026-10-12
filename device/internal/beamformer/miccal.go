package beamformer

// Per-unit mic calibration for the wake beam.
//
// A superdirective sum differences mics that are 36mm apart, so a gain error
// between capsules shows up directly in what it passes. Amazon's units carry a
// per-mic value in /proc/idme/miccal.N (N is the mic's ALSA channel); Radar's
// AFE reads it and logs "calibration data is 0, use default value" for a zero,
// with 16384 as the default (asp.cfg: Calibration.Default value). That makes it
// a Q14 fixed-point gain with 16384 as unity, and measured on one unit the
// seven values are 15483 to 17018: all within +/-0.5dB of each other.
//
// What the file does NOT say is which way the gain applies: whether a mic that
// reads 5% high is stored as 17203 (so its signal is multiplied by 1.05) or as
// 15601. This applies the value as a multiplier, and logs the ambient level of
// every mic raw, multiplied and divided at the start so the direction can be
// read off a device; EM_MIC_CAL=invert divides instead and EM_MIC_CAL=off
// ignores the file. With +/-0.5dB spreads the cost of the wrong direction is
// at most about 1dB on the worst mic.

import (
	"fmt"
	"log"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

const (
	micCalUnity = 16384.0
	// A value outside this range of unity is a fault in the file, not a
	// capsule: it is ignored, and so is the rest of the set.
	micCalMin, micCalMax = 0.5, 2.0
	// micCheckSamples is how long the level check listens: 5s. Counted in
	// samples because it is fed whole mic batches (160ms), not 8ms blocks;
	// counting calls made it a 100s check.
	micCheckSamples = 5 * sampleRate
)

// micCalDir is where the values are read from. A variable so the tests can
// point it at a directory.
var micCalDir = "/proc/idme"

// loadMicCal returns one linear gain per channel, 1 where nothing usable was
// found, and a one-line account of what it did.
func loadMicCal(dir string) (gains [NumMics]float64, note string) {
	for i := range gains {
		gains[i] = 1
	}
	mode := strings.ToLower(os.Getenv("EM_MIC_CAL"))
	if mode == "off" {
		return gains, "ignored (EM_MIC_CAL=off)"
	}
	var raw [NumMics]float64
	var missing []string
	// firstProblem is why the first unusable value was unusable, so a set that
	// is all "missing" says what was wrong with it: an unreadable file and an
	// unparseable one look identical otherwise, and the second cost a
	// calibration that read as "not available" on a unit that has it.
	var firstProblem string
	for ch := 0; ch < NumMics; ch++ {
		b, err := os.ReadFile(filepath.Join(dir, fmt.Sprintf("miccal.%d", ch)))
		if err != nil {
			missing = append(missing, strconv.Itoa(ch))
			if firstProblem == "" {
				firstProblem = err.Error()
			}
			continue
		}
		// idme values are NUL-terminated (pkg/board does the same for the
		// device type), and TrimSpace leaves a NUL in place.
		text := strings.TrimRight(string(b), "\x00 \t\r\n")
		v, err := strconv.ParseInt(text, 0, 64)
		if err != nil || v == 0 {
			// The AFE's own rule for a zero: use the default.
			missing = append(missing, strconv.Itoa(ch))
			if firstProblem == "" {
				firstProblem = fmt.Sprintf("mic %d holds %q", ch, string(b))
			}
			continue
		}
		raw[ch] = float64(v)
	}
	if len(missing) == NumMics {
		return gains, "not available, mics left uncalibrated (" + firstProblem + ")"
	}
	var g [NumMics]float64
	for ch := range g {
		if raw[ch] == 0 {
			g[ch] = 1
			continue
		}
		g[ch] = raw[ch] / micCalUnity
		if mode == "invert" {
			g[ch] = micCalUnity / raw[ch]
		}
		if g[ch] < micCalMin || g[ch] > micCalMax {
			return gains, fmt.Sprintf("mic %d reads %.0f, outside %.1f-%.1fx of unity: whole set ignored", ch, raw[ch], micCalMin, micCalMax)
		}
	}
	how := "multiplied"
	if mode == "invert" {
		how = "divided"
	}
	note = fmt.Sprintf("%s, %s", how, formatDB(g[:]))
	if len(missing) > 0 {
		note += " (no value for mic " + strings.Join(missing, ", ") + ")"
	}
	return g, note
}

func formatDB(g []float64) string {
	s := make([]string, len(g))
	for i, v := range g {
		s[i] = fmt.Sprintf("%+.2f", 20*math.Log10(v))
	}
	return strings.Join(s, " ") + " dB"
}

// micCheck measures the ambient level of every mic over the first few seconds
// the beam runs, and says how flat it looks raw, with the calibration
// multiplied in and with it divided in. Ambient sound is close to diffuse, so
// the right correction is the one that flattens the levels; it is logged, not
// acted on, because a TV on one side flattens nothing and must not choose.
type micCheck struct {
	sum  [NumMics]float64
	n    int
	done bool
}

func (c *micCheck) add(ch int, x float32) { c.sum[ch] += float64(x) * float64(x) }

func (c *micCheck) block(samples int) {
	c.n += samples
	if c.n >= micCheckSamples {
		c.done = true
	}
}

func (c *micCheck) report(chans []int, gains [NumMics]float64) {
	spread := func(f func(ch int) float64) float64 {
		lo, hi := math.Inf(1), math.Inf(-1)
		for _, ch := range chans {
			v := 10 * math.Log10(math.Max(c.sum[ch]/float64(c.n), 1e-9)*f(ch)*f(ch))
			lo, hi = math.Min(lo, v), math.Max(hi, v)
		}
		return hi - lo
	}
	log.Printf("[beam] mic level check over %.0fs, spread across mics %v: raw %.2fdB, with the calibration as applied %.2fdB, with it the other way round %.2fdB",
		float64(c.n)/sampleRate, chans,
		spread(func(int) float64 { return 1 }),
		spread(func(ch int) float64 { return gains[ch] }),
		spread(func(ch int) float64 { return 1 / gains[ch] }))
}
