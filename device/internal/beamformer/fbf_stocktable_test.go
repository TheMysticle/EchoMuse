package beamformer

import (
	"fmt"
	"math"
	"math/cmplx"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const realStockTable = "/mnt/Development/alexa-firm/out/system/system/vendor/etc/audio-algorithms/coefs_FBFV2_4micsSq_8beams_idnr_m15_lowLatency.cfg"

// writeTable writes a table in the file's format with the given value at each
// index.
func writeTable(t *testing.T, val func(i int) float64) string {
	t.Helper()
	var sb strings.Builder
	sb.WriteString("/* test table */\n\n")
	for i := 0; i < fbfBeams*len(fbfChannels)*stockTableBands*stockTableTaps*2; i++ {
		fmt.Fprintf(&sb, "%.12f,\n", val(i))
	}
	p := filepath.Join(t.TempDir(), "table.cfg")
	if err := os.WriteFile(p, []byte(sb.String()), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

// A tap is a delay of whole hops, so at bin k it is a phase of pi*k*t: taps 1
// and 3 flip sign at odd bins and taps 0 and 2 never do.
func TestStockTableFoldsTapsAtTheBinCentres(t *testing.T) {
	// beam 2, mic 1, bands 3 and 4: real parts 1, 10, 100, 1000 on taps 0..3.
	const beam, mic = 2, 1
	p := writeTable(t, func(i int) float64 {
		j := i / 2
		tap := j % stockTableTaps
		band := (j / stockTableTaps) % stockTableBands
		m := (j / stockTableTaps / stockTableBands) % len(fbfChannels)
		b := j / stockTableTaps / stockTableBands / len(fbfChannels)
		if b != beam || m != mic || (band != 3 && band != 4) {
			return 0
		}
		if i%2 == 1 {
			return float64(tap) // imaginary part: 0, 1, 2, 3
		}
		return []float64{1, 10, 100, 1000}[tap]
	})
	w, err := loadStockTable(p)
	if err != nil {
		t.Fatal(err)
	}
	// odd bin 3: taps 1 and 3 flipped. 1-10+100-1000, imag 0-1+2-3.
	if got, want := w.W[beam][mic][3], complex64(complex(-909, -2)); got != want {
		t.Errorf("bin 3 = %v, want %v", got, want)
	}
	// even bin 4: all add. 1+10+100+1000, imag 0+1+2+3.
	if got, want := w.W[beam][mic][4], complex64(complex(1111, 6)); got != want {
		t.Errorf("bin 4 = %v, want %v", got, want)
	}
	if w.W[beam][mic][5] != 0 || w.W[0][0][3] != 0 {
		t.Error("weights appeared where the table has none")
	}
}

func TestStockTableRefusesWhatIsNotTheTable(t *testing.T) {
	if _, err := loadStockTable(filepath.Join(t.TempDir(), "missing")); err == nil {
		t.Error("a missing file loaded")
	}
	p := filepath.Join(t.TempDir(), "short.cfg")
	os.WriteFile(p, []byte("0.5,\n0.25,\n"), 0o644)
	if _, err := loadStockTable(p); err == nil {
		t.Error("a short table loaded")
	}
}

// On the real table (when this machine has the dump): every beam must have a
// distinct, sharp peak, and read through the same stage as ours it must agree
// with our design's look directions up to the half turn the conventions leave.
func TestRealStockTableMakesEightBeams(t *testing.T) {
	if _, err := os.Stat(realStockTable); err != nil {
		t.Skip("no stock dump here")
	}
	st, err := loadStockTable(realStockTable)
	if err != nil {
		t.Fatal(err)
	}
	own := fbfDesign()
	peak := func(w *SDWeights, b int) (az, gain float64) {
		for a := 0.0; a < 360; a++ {
			var g float64
			for _, f := range []float64{1000, 1500, 2000, 2500} {
				g += cmplx.Abs(w.Response(b, f, a, 0))
			}
			if g > gain {
				gain, az = g, a
			}
		}
		return az, gain / 4
	}
	for b := 0; b < fbfBeams; b++ {
		sa, sg := peak(st, b)
		oa, og := peak(own, b)
		t.Logf("beam %d: stock peak %3.0f deg gain %.2f | ours peak %3.0f deg gain %.2f (look %.0f)", b, sa, sg, oa, og, own.Azimuths[b])
		if sg < 0.8 || sg > 1.3 {
			t.Errorf("beam %d: stock gain %.2f at its peak, want about unity", b, sg)
		}
		if d := math.Abs(angleDiff(sa, oa)); d > 20 {
			t.Errorf("beam %d: stock peaks %.0f deg from ours, so the fold's convention is not the one that reproduces the beams", b, d)
		}
	}
}
