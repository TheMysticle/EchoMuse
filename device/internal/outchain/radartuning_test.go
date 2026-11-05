package outchain

import (
	"github.com/wilbowes/EchoMuse/pkg/board"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// radarTuningForTest is a Radar's stock tuning, from the directory named by
// ECHOMUSE_RADAR_TUNING: a copy of an Echo 2's
// /system/vendor/etc/audio-algorithms. The files are not in this repository,
// so without it the test is skipped.
func radarTuningForTest(t testing.TB) *RadarTuning {
	t.Helper()
	dir := os.Getenv("ECHOMUSE_RADAR_TUNING")
	if dir == "" {
		t.Skip("ECHOMUSE_RADAR_TUNING not set: needs a copy of a Radar's audio-algorithms")
	}
	rt, err := LoadRadarTuning(dir)
	if err != nil {
		t.Fatal(err)
	}
	return rt
}

func TestLoadRadarTuningReadsWhatAFEListsAndNothingElse(t *testing.T) {
	rt, err := LoadRadarTuning(filepath.Join("testdata", "radar_tuning"))
	if err != nil {
		t.Fatal(err)
	}
	wantFIR := [][]float64{{1, -0.25, 0.125, 0}, {0.5, 0.25, 0, 0}}
	if !reflect.DeepEqual(rt.FIRBands, wantFIR) || !reflect.DeepEqual(rt.FIRBounds, []float64{40, 100}) {
		t.Errorf("fir = %v at %v", rt.FIRBands, rt.FIRBounds)
	}
	wantPEQ := []tuningBiquad{{"LOW_SHELF", 120, 0.7, 3}, {"PEAK", 90, 1.2, 1}}
	if !reflect.DeepEqual(rt.PEQ, wantPEQ) {
		t.Errorf("peq = %+v, want the two that are not BYPASS", rt.PEQ)
	}
	if rt.TrimDb != 1.5 {
		t.Errorf("trim = %g", rt.TrimDb)
	}
	m := rt.MBCL
	if m == nil || m.InVol != 2 || !reflect.DeepEqual(m.FC, []float64{80, 300, 4000}) ||
		m.Bands[2] != (mbclBand{2, 2, -12, -30, 1, -5, 50}) || m.FullBand != (mbclLimiterSpec{0, -2, 30}) {
		t.Errorf("mbcl = %+v", m)
	}

	// The chain takes all of it: the MBCL in place of the guard and the
	// limiter, the curve available to StockCurve.
	c := NewWithTuning(48000, DefaultGuardBand, board.BiscuitVoicing(), rt)
	st := c.stock
	if st == nil || st.mbcl == nil || st.lim == nil || st.lim.thresholdDb != -2 {
		t.Fatalf("stock = %#v", st)
	}
	if len(st.firBands) != 2 || len(st.peq) != 2 || st.trimGain != dbToGain(1.5) {
		t.Errorf("stock curve: %d bands, %d biquads, trim %g", len(st.firBands), len(st.peq), st.trimGain)
	}
}

// A stage AFE.cfg does not list is not loaded, even with its file present.
func TestLoadRadarTuningSkipsUnlistedStages(t *testing.T) {
	dir := t.TempDir()
	for _, f := range []string{"EQ_quiet.cfg", "EQ_loud.cfg", "PEQ.cfg", "MBCL.cfg"} {
		copyFile(t, filepath.Join("testdata", "radar_tuning", f), filepath.Join(dir, f))
	}
	afe := `{"Path Definition": {"Playback": {"Algorithms": {"MBCL": "MBCL"}}},
	         "Algorithm Definition": {"MBCL": {"External Config": ["MBCL.cfg"]}}}`
	if err := os.WriteFile(filepath.Join(dir, "AFE.cfg"), []byte(afe), 0o644); err != nil {
		t.Fatal(err)
	}
	rt, err := LoadRadarTuning(dir)
	if err != nil {
		t.Fatal(err)
	}
	if rt.MBCL == nil || rt.FIRBands != nil || rt.PEQ != nil || rt.TrimDb != 0 {
		t.Errorf("got %s, want the MBCL only", rt)
	}
}

// A listed stage whose file is missing fails the whole load: half the
// tuning is a different sound from the one the files describe.
func TestLoadRadarTuningFailsOnAMissingFile(t *testing.T) {
	dir := t.TempDir()
	for _, f := range []string{"AFE.cfg", "EQ_quiet.cfg", "PEQ.cfg", "MBCL.cfg"} {
		copyFile(t, filepath.Join("testdata", "radar_tuning", f), filepath.Join(dir, f))
	}
	if rt, err := LoadRadarTuning(dir); err == nil {
		t.Fatalf("loaded %s without EQ_loud.cfg", rt)
	}
}

// Without a tuning a chain runs the board's voicing, with no stock curve,
// multiband or stock limiter.
func TestChainWithoutTuning(t *testing.T) {
	c := NewWithTuning(48000, DefaultGuardBand, board.BiscuitVoicing(), nil)
	if c.stock != nil || !c.voiced() {
		t.Error("chain without a tuning")
	}
}

func copyFile(t *testing.T, from, to string) {
	t.Helper()
	data, err := os.ReadFile(from)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(to, data, 0o644); err != nil {
		t.Fatal(err)
	}
}

// With a plug in, chainForJack turns the stock profile off: a Radar chain
// leaves out its tuning, which is for its speaker, and takes it back when
// the plug comes out.
func TestRadarJackLeavesOutTheSpeakerStages(t *testing.T) {
	rt, err := LoadRadarTuning(filepath.Join("testdata", "radar_tuning"))
	if err != nil {
		t.Fatal(err)
	}
	c := NewWithTuning(48000, DefaultGuardBand, board.BiscuitVoicing(), rt)
	c.SetActive(true)
	wide := make([]float64, 4096)
	for i := range wide {
		wide[i] = 30000 * math.Sin(float64(i/2)*2*math.Pi*60/48000)
	}
	out := make([]byte, 8192)
	p := DefaultParams()
	p.StockSound = false
	c.SetParams(p)
	c.ProcessWide(wide, out)
	if c.tuned() || c.stock.fir != nil {
		t.Fatal("jack: the tuning ran")
	}
	if r := c.stock.mbcl.takeMaxReductionDb(); r != 0 {
		t.Errorf("jack: the multiband ran (%gdB)", r)
	}

	p.StockSound = true
	c.SetParams(p)
	c.ProcessWide(wide, out)
	if !c.tuned() || c.stock.fir == nil {
		t.Fatal("speaker: the tuning did not come back")
	}
}

// The log line names what a Radar chain actually runs: MBCL's limiter, not
// the one in Params, and whether the stock curve is in.
func TestDescribeNamesWhatRadarRuns(t *testing.T) {
	rt, err := LoadRadarTuning(filepath.Join("testdata", "radar_tuning"))
	if err != nil {
		t.Fatal(err)
	}
	c := NewWithTuning(48000, DefaultGuardBand, board.BiscuitVoicing(), rt)
	p := DefaultParams()
	if got, want := c.Describe(p),
		"eq=flat speech_boost=off guard=-30dB limiter=-2dB/30ms stock_sound=on stock_tuning=on stock_curve=on"; got != want {
		t.Errorf("speaker: got %q, want %q", got, want)
	}
	p.StockCurve = false
	if got, want := c.Describe(p),
		"eq=flat speech_boost=off guard=-30dB limiter=-2dB/30ms stock_sound=on stock_tuning=on stock_curve=off"; got != want {
		t.Errorf("curve off: got %q, want %q", got, want)
	}
	p.StockSound = false
	if got, want := c.Describe(p), p.String(); got != want {
		t.Errorf("jack: got %q, want %q", got, want)
	}
	if got, want := New(48000).Describe(DefaultParams()), DefaultParams().String(); got != want {
		t.Errorf("biscuit: got %q, want %q", got, want)
	}
}
