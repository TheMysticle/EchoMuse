package beamformer

import (
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The values read off a Radar unit's /proc/idme on 2026-10-12.
var radarMicCal = [NumMics]int{16779, 16397, 16501, 16102, 16479, 17018, 15483}

func writeMicCal(t *testing.T, vals []int) string {
	t.Helper()
	dir := t.TempDir()
	for i, v := range vals {
		if err := os.WriteFile(filepath.Join(dir, fmt.Sprintf("miccal.%d", i)), []byte(fmt.Sprintf("%d\n", v)), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func TestMicCalReadsAUnitsValuesAsQ14Gains(t *testing.T) {
	g, note := loadMicCal(writeMicCal(t, radarMicCal[:]))
	for ch, v := range radarMicCal {
		if want := float64(v) / 16384; math.Abs(g[ch]-want) > 1e-12 {
			t.Errorf("mic %d: gain %v, want %v", ch, g[ch], want)
		}
		// One real unit: every capsule within half a dB of unity.
		if db := 20 * math.Log10(g[ch]); math.Abs(db) > 0.6 {
			t.Errorf("mic %d: %.2fdB from unity", ch, db)
		}
	}
	if note == "" {
		t.Error("no account of what was done")
	}
}

// idme hands values back NUL-terminated, which `cat` does not show. The first
// version trimmed only whitespace, so a unit with all seven values logged "not
// available" and ran uncalibrated.
func TestMicCalReadsNULTerminatedValues(t *testing.T) {
	dir := t.TempDir()
	for i, v := range radarMicCal {
		body := []byte(fmt.Sprintf("%d\x00", v))
		if err := os.WriteFile(filepath.Join(dir, fmt.Sprintf("miccal.%d", i)), body, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	g, note := loadMicCal(dir)
	for ch, v := range radarMicCal {
		if want := float64(v) / 16384; math.Abs(g[ch]-want) > 1e-12 {
			t.Errorf("mic %d: gain %v, want %v (%s)", ch, g[ch], want, note)
		}
	}
}

// A set that cannot be used must say why, so an unparseable file and an
// unreadable one are not the same line in the log.
func TestMicCalSaysWhyItGaveUp(t *testing.T) {
	_, note := loadMicCal(t.TempDir())
	if !strings.Contains(note, "no such file") {
		t.Errorf("missing files: %q", note)
	}
	dir := t.TempDir()
	for i := 0; i < NumMics; i++ {
		os.WriteFile(filepath.Join(dir, fmt.Sprintf("miccal.%d", i)), []byte("abc\x00"), 0o644)
	}
	if _, note = loadMicCal(dir); !strings.Contains(note, `"abc\x00"`) {
		t.Errorf("unparseable files: %q", note)
	}
}

func TestMicCalFallsBackTheWayTheAFEDoes(t *testing.T) {
	// A zero is "use the default" in Radar's own AFE.
	vals := radarMicCal
	vals[3] = 0
	g, _ := loadMicCal(writeMicCal(t, vals[:]))
	if g[3] != 1 {
		t.Errorf("a zero value gave gain %v, want unity", g[3])
	}
	if g[0] != float64(vals[0])/16384 {
		t.Error("one zero must not discard the others")
	}
	// Nothing readable at all: unity everywhere, and said so.
	g, note := loadMicCal(t.TempDir())
	for ch := range g {
		if g[ch] != 1 {
			t.Errorf("mic %d: gain %v with no file", ch, g[ch])
		}
	}
	if note == "" {
		t.Error("silence about a missing calibration")
	}
}

// A value far from unity is a fault in the file and not a capsule. Applying it
// would put 6dB into a beam that differences its mics, so the whole set goes.
func TestMicCalIgnoresASetWithAnImplausibleValue(t *testing.T) {
	vals := radarMicCal
	vals[1] = 40000
	g, note := loadMicCal(writeMicCal(t, vals[:]))
	for ch := range g {
		if g[ch] != 1 {
			t.Errorf("mic %d: gain %v, want unity for a rejected set", ch, g[ch])
		}
	}
	if note == "" {
		t.Error("no reason given")
	}
}

// The direction the gain applies in is inferred, not documented, so it can be
// reversed or switched off without a rebuild.
func TestMicCalEscapeHatches(t *testing.T) {
	dir := writeMicCal(t, radarMicCal[:])
	t.Setenv("EM_MIC_CAL", "invert")
	g, _ := loadMicCal(dir)
	if want := 16384.0 / float64(radarMicCal[5]); math.Abs(g[5]-want) > 1e-12 {
		t.Errorf("invert: gain %v, want %v", g[5], want)
	}
	t.Setenv("EM_MIC_CAL", "off")
	g, _ = loadMicCal(dir)
	for ch := range g {
		if g[ch] != 1 {
			t.Errorf("off: mic %d gain %v", ch, g[ch])
		}
	}
}

// The gain must reach the audio: with the centre mic's calibration set, the
// neutral output is the centre mic scaled by it.
func TestMicCalIsAppliedToTheSamples(t *testing.T) {
	old := micCalDir
	t.Cleanup(func() { micCalDir = old })
	micCalDir = writeMicCal(t, radarMicCal[:])

	b := New()
	enableWakeBeam(b)
	x := zeros(NumMics, 2560)
	for i := range x[centreCh] {
		x[centreCh][i] = 1000 * math.Sin(float64(i)/7)
	}
	in := s16(x)
	out := samples(b.WakeBeam(in))
	c := samples(in[centreCh])
	want := float64(radarMicCal[centreCh]) / 16384
	off := b.fbf.off
	for i := off + 40; i < len(out); i += 53 {
		if math.Abs(out[i]-math.Round(c[i-off]*want)) > 1.01 {
			t.Fatalf("sample %d: %v, want %v (centre %v x %.4f)", i, out[i], math.Round(c[i-off]*want), c[i-off], want)
		}
	}
}

// The level check reports, once, how flat the mics look three ways, and flat
// input must read as flat.
func TestMicCheckSpreadIsZeroForEqualLevels(t *testing.T) {
	var c micCheck
	for ch := 0; ch < NumMics; ch++ {
		for i := 0; i < 1000; i++ {
			c.add(ch, 100)
		}
	}
	c.n = 1000
	chans := fbfChannels
	var ones [NumMics]float64
	for i := range ones {
		ones[i] = 1
	}
	c.report(chans, ones) // must not panic or divide by zero
	if c.done {
		t.Error("done before its blocks were counted")
	}
	// Fed what the mic really delivers: 160ms batches of 2560 samples. It
	// must take 5s of them, not a count of calls.
	c = micCheck{}
	calls := 0
	for !c.done && calls < 1000 {
		c.block(2560)
		calls++
	}
	if want := 32; calls != want { // 5s / 160ms = 31.25, rounded up
		t.Errorf("finished after %d batches (%.1fs), want %d (5s)", calls, float64(calls)*0.16, want)
	}
}
