package outchain

import (
	"bytes"
	"compress/gzip"
	"encoding/binary"
	"encoding/json"
	"io"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wilbowes/EchoMuse/pkg/board"
)

// vectorParams is the manifest's parameter object, as em_db names the keys.
type vectorParams struct {
	Bands            []float64 `json:"bands"`
	Loudness         bool      `json:"loudness"`
	GuardEnabled     bool      `json:"guardEnabled"`
	GuardDb          float64   `json:"guardDb"`
	LimiterEnabled   bool      `json:"limiterEnabled"`
	LimiterThreshold float64   `json:"limiterThreshold"`
	LimiterRelease   float64   `json:"limiterRelease"`
	StockSound       bool      `json:"stockSound"`
	StockCurve       bool      `json:"stockCurve"`
	// VolumeGain is the volume ahead of a Radar stock-tuning chain. Absent on
	// every case that predates it, and then never set.
	VolumeGain *float64 `json:"volumeGain"`
}

func (v vectorParams) params() Params {
	p := Params{
		Loudness:           v.Loudness,
		GuardEnabled:       v.GuardEnabled,
		GuardDb:            v.GuardDb,
		LimiterEnabled:     v.LimiterEnabled,
		LimiterThresholdDb: v.LimiterThreshold,
		LimiterReleaseMs:   v.LimiterRelease,
		StockSound:         v.StockSound,
		StockCurve:         v.StockCurve,
	}
	copy(p.Bands[:], v.Bands)
	return p
}

type vectorCase struct {
	Name       string `json:"name"`
	Chunk      int    `json:"chunk"`
	SampleRate int    `json:"sampleRate"`
	Chunks     int    `json:"chunks"`
	// Board "radar" runs the Echo's own stock tuning (NewWithTuning), which
	// needs a copy of a Radar's audio-algorithms (radarTuningForTest).
	// Empty is biscuit.
	Board    string `json:"board"`
	Schedule [][2]json.RawMessage
	Stats    struct {
		GuardReductionDb   float64 `json:"guardReductionDb"`
		LimiterReductionDb float64 `json:"limiterReductionDb"`
		Clipped            uint64  `json:"clipped"`
		ClippedBypassed    uint64  `json:"clippedBypassed"`
	} `json:"stats"`
}

func readS16(t *testing.T, path string) []int16 {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := io.ReadAll(gz)
	if err != nil {
		t.Fatal(err)
	}
	out := make([]int16, len(raw)/2)
	for i := range out {
		out[i] = int16(binary.LittleEndian.Uint16(raw[i*2:]))
	}
	return out
}

func stereo(mono []int16) []byte {
	buf := make([]byte, len(mono)*4)
	for i, s := range mono {
		binary.LittleEndian.PutUint16(buf[i*4:], uint16(s))
		binary.LittleEndian.PutUint16(buf[i*4+2:], uint16(s))
	}
	return buf
}

// TestMatchesControllerChain holds the port to em_eq/em_mbc/em_limiter on
// vectors the Python generated (testdata/gen_vectors.py), period by period,
// with every parameter change applied where the device would apply it.
//
// Tolerance is ONE LSB on a handful of samples, and it exists for one
// reason: numpy vectorises the release recursions as a sheared running
// minimum and computes 10**x its own way, so the float64 gain differs from
// ours in the last bits, and a sample sitting on an integer boundary can
// truncate either side of it. Anything larger is a behaviour difference.
func TestMatchesControllerChain(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("testdata", "vectors.json"))
	if err != nil {
		t.Fatal(err)
	}
	var cases []vectorCase
	if err := json.Unmarshal(raw, &cases); err != nil {
		t.Fatal(err)
	}
	if len(cases) == 0 {
		t.Fatal("no vectors")
	}

	for _, vc := range cases {
		t.Run(vc.Name, func(t *testing.T) {
			in := readS16(t, filepath.Join("testdata", vc.Name+".in.s16.gz"))
			want := readS16(t, filepath.Join("testdata", vc.Name+".out.s16.gz"))
			if len(in) != vc.Chunks*vc.Chunk || len(want) != len(in) {
				t.Fatalf("vector lengths: in=%d want=%d, expected %d",
					len(in), len(want), vc.Chunks*vc.Chunk)
			}

			if vc.Board == "radar" {
				runRadarVector(t, vc, in, want)
				return
			}
			sched := map[int]Params{}
			for _, e := range vc.Schedule {
				var at int
				var vp vectorParams
				if err := json.Unmarshal(e[0], &at); err != nil {
					t.Fatal(err)
				}
				if err := json.Unmarshal(e[1], &vp); err != nil {
					t.Fatal(err)
				}
				sched[at] = vp.params()
			}

			c := New(vc.SampleRate)
			c.SetActive(true)
			got := make([]int16, 0, len(in))
			for k := 0; k < vc.Chunks; k++ {
				if p, ok := sched[k]; ok {
					c.SetParams(p)
				}
				buf := stereo(in[k*vc.Chunk : (k+1)*vc.Chunk])
				c.Process(buf)
				for i := 0; i < len(buf); i += 4 {
					l := int16(binary.LittleEndian.Uint16(buf[i:]))
					r := int16(binary.LittleEndian.Uint16(buf[i+2:]))
					if l != r {
						t.Fatalf("chunk %d frame %d: L=%d R=%d", k, i/4, l, r)
					}
					got = append(got, l)
				}
			}

			off, worst := 0, 0
			for i := range want {
				d := int(got[i]) - int(want[i])
				if d != 0 {
					off++
				}
				if d < 0 {
					d = -d
				}
				if d > worst {
					worst = d
					if d > 1 {
						t.Errorf("sample %d (chunk %d): got %d want %d",
							i, i/vc.Chunk, got[i], want[i])
					}
				}
			}
			// One in a thousand is generous for last-bit rounding and far
			// short of anything systematic.
			if off > len(want)/1000 {
				t.Errorf("%d of %d samples differ (worst %d LSB)", off, len(want), worst)
			}
			t.Logf("%d of %d samples differ by 1 LSB", off, len(want))

			st := c.TakeStats()
			if math.Abs(st.GuardReductionDb-vc.Stats.GuardReductionDb) > 1e-6 {
				t.Errorf("guard reduction %.6f, want %.6f", st.GuardReductionDb, vc.Stats.GuardReductionDb)
			}
			if math.Abs(st.LimiterReductionDb-vc.Stats.LimiterReductionDb) > 1e-6 {
				t.Errorf("limiter reduction %.6f, want %.6f", st.LimiterReductionDb, vc.Stats.LimiterReductionDb)
			}
			if st.Clipped != vc.Stats.Clipped || st.ClippedBypassed != vc.Stats.ClippedBypassed {
				t.Errorf("clipped %d/%d bypassed, want %d/%d",
					st.Clipped, st.ClippedBypassed, vc.Stats.Clipped, vc.Stats.ClippedBypassed)
			}
		})
	}
}

func TestWidePeriodKeepsChainStateInNormalisedDomain(t *testing.T) {
	reference, switched := New(48000), New(48000)
	reference.SetActive(true)
	switched.SetActive(true)

	for chunk := 0; chunk < 3; chunk++ {
		mono := make([]int16, 512)
		for i := range mono {
			n := chunk*len(mono) + i
			mono[i] = int16(12000 * math.Sin(2*math.Pi*80*float64(n)/48000))
		}
		want := stereo(mono)
		reference.Process(want)

		if chunk == 1 {
			const scale = 4.0
			wide := make([]float64, len(mono)*2)
			scales := make([]float64, len(mono))
			for i, sample := range mono {
				wide[i*2], wide[i*2+1] = float64(sample)*scale, float64(sample)*scale
				scales[i] = scale
			}
			switched.ProcessFloat(wide, scales)
			for i := range mono {
				got := wide[i*2] / scale
				expected := float64(int16(binary.LittleEndian.Uint16(want[i*4:])))
				if math.Abs(got-expected) > 1 {
					t.Fatalf("wide frame %d: normalised %.3f, want %.3f", i, got, expected)
				}
			}
			continue
		}

		got := stereo(mono)
		switched.Process(got)
		if string(got) != string(want) {
			t.Fatalf("ordinary chunk %d changed across wide-period transition", chunk)
		}
	}
}

// BenchmarkPeriod is one 2048-frame period through the whole chain, shaped
// EQ plus speech boost, guard and limiter all on — the most work it can do.
// A period is 42.7ms of audio; ns/op against that is the realtime share.
func BenchmarkPeriod(b *testing.B) {
	p := DefaultParams()
	p.Bands = [NumBands]float64{6, 3, 0, -2, 0, 2, 4, 1}
	p.Loudness = true
	benchPeriod(b, p)
}

// BenchmarkPeriodDefaults is the chain as the fleet runs it: flat EQ, guard
// and limiter on — the common case, where the EQ costs nothing.
func BenchmarkPeriodDefaults(b *testing.B) { benchPeriod(b, DefaultParams()) }

func benchPeriod(b *testing.B, p Params) {
	c := New(48000)
	c.SetActive(true)
	c.SetParams(p)
	mono := make([]int16, 2048)
	for i := range mono {
		mono[i] = int16(20000 * math.Sin(2*math.Pi*80*float64(i)/48000))
	}
	src := stereo(mono)
	buf := make([]byte, len(src))
	b.SetBytes(int64(len(src)))
	for i := 0; i < b.N; i++ {
		copy(buf, src)
		c.Process(buf)
	}
}

// ProcessWide at unity must give exactly what Process gives: it is the same
// chain, fed floats instead of S16, and the stock-sound profile relies on it
// matching the vectors.
func TestProcessWideAtUnityMatchesProcess(t *testing.T) {
	p := DefaultParams()
	p.StockSound = true
	a, b := New(48000), New(48000)
	for _, c := range []*Chain{a, b} {
		c.SetParams(p)
		c.SetActive(true)
	}
	for period := 0; period < 6; period++ {
		buf := make([]byte, 2048*4)
		wide := make([]float64, 2048*2)
		for i := 0; i < 2048; i++ {
			v := int16(20000 * math.Sin(2*math.Pi*220*float64(period*2048+i)/48000))
			buf[i*4], buf[i*4+1], buf[i*4+2], buf[i*4+3] = byte(uint16(v)), byte(uint16(v)>>8), byte(uint16(v)), byte(uint16(v)>>8)
			wide[i*2], wide[i*2+1] = float64(v), float64(v)
		}
		out := make([]byte, len(buf))
		a.Process(buf)
		b.ProcessWide(wide, out)
		if !bytes.Equal(buf, out) {
			t.Fatalf("period %d: ProcessWide differs from Process", period)
		}
	}
}

// Over full scale goes in, at most full scale comes out: the profile's top
// volume steps rely on the limiter, and the clip is the backstop.
func TestProcessWideNeverExceedsFullScale(t *testing.T) {
	p := DefaultParams()
	p.StockSound = true
	c := New(48000)
	c.SetParams(p)
	c.SetActive(true)
	wide := make([]float64, 2048*2)
	for i := 0; i < 2048; i++ {
		v := 4 * 32767 * math.Sin(2*math.Pi*1000*float64(i)/48000) // +12dB over
		wide[i*2], wide[i*2+1] = v, v
	}
	out := make([]byte, 2048*4)
	for k := 0; k < 4; k++ {
		c.ProcessWide(wide, out)
	}
	for i := 0; i < 2048; i++ {
		s := int16(uint16(out[i*4]) | uint16(out[i*4+1])<<8)
		if s == math.MinInt16 {
			t.Fatalf("frame %d hit the negative rail", i)
		}
	}
}

// The stock-sound voicing was tuned with the guard on, so a stored
// bassGuardEnabled=false must not turn it off there; the plain chain (the
// jack, older controllers) still honours it.
func TestStockSoundKeepsTheGuardOn(t *testing.T) {
	c := New(48000)
	p := DefaultParams()
	p.GuardEnabled = false
	c.apply(p)
	if !c.guard.enabled {
		t.Error("stock sound: guard turned off by config")
	}
	p.StockSound = false
	c.apply(p)
	if c.guard.enabled {
		t.Error("plain chain: guard should follow config")
	}
}

// A voicing's volume EQ follows SetVolume from the next period, and leaves
// a voicing without one untouched.
func TestVolumeEQFollowsSetVolume(t *testing.T) {
	v := *board.BiscuitVoicing()
	v.ByVolume = []board.VolumeEQ{{Hz: 1000, Q: 1, Gains: []board.VolumeGain{{Percent: 50, Db: 10}, {Percent: 90, Db: 0}}}}
	level := func(vc *board.Voicing, lv int) float64 {
		c := NewFor(48000, DefaultGuardBand, vc)
		c.SetActive(true)
		c.SetVolume(lv)
		p := DefaultParams()
		p.GuardEnabled, p.LimiterEnabled = false, false
		c.SetParams(p)
		const n = 48000
		buf := make([]byte, n*4)
		for i := 0; i < n; i++ {
			s := int16(300 * math.Sin(2*math.Pi*1000*float64(i)/48000))
			binary.LittleEndian.PutUint16(buf[i*4:], uint16(s))
			binary.LittleEndian.PutUint16(buf[i*4+2:], uint16(s))
		}
		c.Process(buf)
		var sum float64
		for i := n / 2; i < n; i++ {
			x := float64(int16(binary.LittleEndian.Uint16(buf[i*4:])))
			sum += x * x
		}
		return 10 * math.Log10(sum/float64(n/2))
	}
	low, high := level(&v, 57), level(&v, 127) // 45%, 100%
	if d := low - high; math.Abs(d-10) > 0.5 {
		t.Errorf("1kHz at 45%% vs 100%%: %+.2fdB, want +10", d)
	}
	b := board.BiscuitVoicing()
	if d := level(b, 57) - level(b, 127); math.Abs(d) > 1e-9 {
		t.Errorf("biscuit moved %+.3gdB with volume through its chain", d)
	}
}

// runRadarVector runs a Radar case the way the speaker does under the stock
// profile: the volume ramped onto the input across the period it changes in,
// then ProcessWide, with the level told to the chain for the FIR's band.
// The reference (em_eq) applies the same ramp on the mono signal; on a
// mono wire the two are the same arithmetic.
func runRadarVector(t *testing.T, vc vectorCase, in, want []int16) {
	sched := map[int]Params{}
	vols := map[int]float64{}
	for _, e := range vc.Schedule {
		var at int
		var vp vectorParams
		if err := json.Unmarshal(e[0], &at); err != nil {
			t.Fatal(err)
		}
		if err := json.Unmarshal(e[1], &vp); err != nil {
			t.Fatal(err)
		}
		p := vp.params()
		p.StockSound = true // the tuning runs only in the stock profile
		sched[at] = p
		if vp.VolumeGain != nil {
			vols[at] = *vp.VolumeGain
		}
	}
	c := NewWithTuning(vc.SampleRate, DefaultGuardBand, board.BiscuitVoicing(), radarTuningForTest(t))
	c.SetActive(true)
	cur, tgt := 1.0, 1.0
	got := make([]int16, 0, len(in))
	for k := 0; k < vc.Chunks; k++ {
		if p, ok := sched[k]; ok {
			c.SetParams(p)
		}
		if g, ok := vols[k]; ok {
			tgt = g
			if k == 0 {
				cur = g // a chain starts at the volume, as the speaker does
			}
			c.SetVolume(int(math.Round(127 + 40*math.Log10(math.Max(g, 1e-9)))))
		}
		mono := in[k*vc.Chunk : (k+1)*vc.Chunk]
		wide := make([]float64, len(mono)*2)
		g, step := cur, 0.0
		if tgt != cur {
			step = (tgt - cur) / float64(len(mono))
		}
		for i, s := range mono {
			if step != 0 {
				g += step
			}
			x := float64(s) * g
			wide[i*2], wide[i*2+1] = x, x
		}
		cur = tgt
		out := make([]byte, len(mono)*4)
		c.ProcessWide(wide, out)
		for i := 0; i < len(out); i += 4 {
			got = append(got, int16(binary.LittleEndian.Uint16(out[i:])))
		}
	}
	off := 0
	for i := range want {
		d := int(got[i]) - int(want[i])
		if d != 0 {
			off++
		}
		if d > 1 || d < -1 {
			t.Fatalf("sample %d (chunk %d): got %d want %d", i, i/vc.Chunk, got[i], want[i])
		}
	}
	if off > len(want)/1000 {
		t.Errorf("%d of %d samples differ by 1 LSB", off, len(want))
	}
	st := c.TakeStats()
	if math.Abs(st.GuardReductionDb-vc.Stats.GuardReductionDb) > 1e-6 ||
		math.Abs(st.LimiterReductionDb-vc.Stats.LimiterReductionDb) > 1e-6 {
		t.Errorf("reductions %.6f/%.6f, want %.6f/%.6f", st.GuardReductionDb, st.LimiterReductionDb,
			vc.Stats.GuardReductionDb, vc.Stats.LimiterReductionDb)
	}
}

// The speaker feeds the chain whole periods, and shorter blocks when a
// low-latency stream has the speaker to itself, in any order. A Radar's
// stock FIR panicked on the shorter block the first time (found on a device
// by pressing Start on a Sendspin stream).
func TestRadarChainTakesVaryingPeriodsInAnyOrder(t *testing.T) {
	rt, err := LoadRadarTuning(filepath.Join("testdata", "radar_tuning"))
	if err != nil {
		t.Fatal(err)
	}
	c := NewWithTuning(48000, DefaultGuardBand, board.BiscuitVoicing(), rt)
	c.SetActive(true)
	c.SetVolume(115)
	for i, frames := range []int{512, 512, 1024, 2048, 512, 2048, 1024} {
		wide := make([]float64, frames*2)
		for j := range wide {
			wide[j] = 8000 * math.Sin(float64(j)*0.01)
		}
		out := make([]byte, frames*4)
		c.ProcessWide(wide, out)
		if i == 0 && !c.tuned() {
			t.Fatal("the stock tuning is not in force under the default params")
		}
	}
	if c.stock.fir == nil || c.stock.fir.period != MaxPeriodFrames {
		t.Fatal("the FIR was not sized for the whole period although the stream opened with a shorter one")
	}
}

// The tuning replaces the voicing; without one, the voicing runs as before.
func TestStockTuningReplacesTheVoicing(t *testing.T) {
	rt, err := LoadRadarTuning(filepath.Join("testdata", "radar_tuning"))
	if err != nil {
		t.Fatal(err)
	}
	c := NewWithTuning(48000, DefaultGuardBand, board.BiscuitVoicing(), rt)
	if !c.HasStockTuning() || !c.tuned() || c.voiced() || c.guard.upper != nil {
		t.Fatal("with a tuning, the voicing's stages must not run")
	}
	if got := c.Describe(c.params); !strings.Contains(got, "stock_tuning=on stock_curve=on") {
		t.Errorf("Describe = %q", got)
	}
	p := DefaultParams()
	p.StockSound = false // a plug in the jack
	c.SetParams(p)
	c.takePending()
	if c.tuned() || c.voiced() {
		t.Error("with the profile off, neither may run")
	}
	v := NewWithTuning(48000, DefaultGuardBand, board.BiscuitVoicing(), nil)
	if v.HasStockTuning() || !v.voiced() {
		t.Error("without a tuning the voicing must run")
	}
	if NewWithTuning(48000, DefaultGuardBand, nil, rt).HasStockTuning() {
		t.Error("a board with no voicing has no stock profile to run a tuning in")
	}
}
