package beamformer

// Stock's own beam table, read from the Echo it is running on.
//
// With wakeBeamStock on, the path can form its eight beams from the table
// Radar's AFE loads (AFE.cfg: "ASR FixedBeamFormerV2"), which is on the
// device's own system partition: /vendor/etc/audio-algorithms on FireOS and on
// emOS, which mounts the same userspace. It is READ there and never copied into
// this repository or the firmware, and when it is absent or not the table we
// expect, the beams we design ourselves stay in use.
//
// Layout (established in tools/bf_design/amazon.go): 32,768 values =
// [beam 8][mic 4][band 128][tap 4][re, im], mics in the AFE's channel map
// order (ch0, 1, 3, 4, which is fbfChannels), bands the 256-point filter
// bank's, 62.5Hz apart, with a hop of 128 samples, so tap t is a delay of t
// hops. At a bin's centre frequency a delay of t hops is a phase of
// 2*pi*(62.5k)*(128t/16000) = pi*k*t, so the four taps fold into one weight
// per bin, the same form this path's spectral stage already applies. The
// sign convention (conjugate, hop phase) cannot be told from magnitudes; the
// alternatives give the same beams up to a half turn, which the group
// selection and the canceller's references (symmetric about the opposite
// beam) do not see.

import (
	"bufio"
	"fmt"
	"log"
	"math"
	"os"
	"regexp"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

// StockTablePath is where the table is on the device; a variable for tests and
// for tools/bf_design, which replays captures on a machine with the dump.
var StockTablePath = "/vendor/etc/audio-algorithms/coefs_FBFV2_4micsSq_8beams_idnr_m15_lowLatency.cfg"

const (
	stockTableBands = 128
	stockTableTaps  = 4
)

var stockTableValue = regexp.MustCompile(`^\s*(-?\d+\.\d+(?:[eE][-+]?\d+)?),?\s*$`)

// loadStockTable reads the table and folds it into per-bin weights.
func loadStockTable(path string) (*SDWeights, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var v []float64
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if m := stockTableValue.FindStringSubmatch(sc.Text()); m != nil {
			x, err := strconv.ParseFloat(m[1], 64)
			if err != nil {
				return nil, err
			}
			v = append(v, x)
		}
	}
	if err := sc.Err(); err != nil {
		return nil, err
	}
	nMics := len(fbfChannels)
	if want := fbfBeams * nMics * stockTableBands * stockTableTaps * 2; len(v) != want {
		return nil, fmt.Errorf("%d values, want %d: not the 8-beam 4-mic table", len(v), want)
	}
	if stockTableBands > araBins {
		return nil, fmt.Errorf("table has %d bands, the stage has %d bins", stockTableBands, araBins)
	}
	pos := make([][2]float64, nMics)
	for i, ch := range fbfChannels {
		pos[i] = MicPosition(ch)
	}
	out := &SDWeights{Mics: nMics, pos: pos, Azimuths: make([]float64, fbfBeams), W: make([][][araBins]complex64, fbfBeams)}
	for b := range out.W {
		out.Azimuths[b] = sdAzimuth(b, fbfBeams)
		out.W[b] = make([][araBins]complex64, nMics)
		for m := 0; m < nMics; m++ {
			for k := 1; k < stockTableBands; k++ {
				var w complex128
				for t := 0; t < stockTableTaps; t++ {
					i := (((b*nMics+m)*stockTableBands+k)*stockTableTaps + t) * 2
					// exp(-i*pi*k*t) is +1 for an even k*t and -1 for an odd one.
					sign := 1.0
					if (k*t)%2 != 0 {
						sign = -1
					}
					w += complex(v[i]*sign, v[i+1]*sign)
				}
				if cr, ci := real(w), imag(w); math.IsNaN(cr) || math.IsNaN(ci) || math.IsInf(cr, 0) || math.IsInf(ci, 0) {
					return nil, fmt.Errorf("beam %d mic %d band %d is not finite", b, m, k)
				}
				out.W[b][m][k] = complex64(w)
			}
		}
	}
	return out, nil
}

var (
	stockTableOnce   sync.Once
	stockTableResult atomic.Pointer[SDWeights]
)

// startStockTable reads the table in the background, once per process; the
// result (nil when it could not be used) is picked up by stockTableReady.
func startStockTable() {
	stockTableOnce.Do(func() {
		go func() {
			start := time.Now()
			w, err := loadStockTable(StockTablePath)
			if err != nil {
				log.Printf("[beam] stock's beam table not used, keeping our own beams: %v", err)
				return
			}
			log.Printf("[beam] stock's beam table read from %s in %v", StockTablePath, time.Since(start).Round(time.Millisecond))
			stockTableResult.Store(w)
		}()
	})
}
