package outchain

import (
	"fmt"
	"math"
	"strings"
	"sync"
	"sync/atomic"

	"github.com/wilbowes/EchoMuse/pkg/board"
)

// Params is the chain's whole configuration. Defaults match
// em_db.DEFAULT_DEVICE_CONFIG.
type Params struct {
	Bands              [NumBands]float64
	Loudness           bool
	GuardEnabled       bool
	GuardDb            float64
	LimiterEnabled     bool
	LimiterThresholdDb float64
	LimiterReleaseMs   float64
	// StockSound adds the board's voicing: the driver correction and the
	// three compressor bands above the guard (#229). It has no effect on a
	// board with no voicing. The device's volume law and order change with
	// it; see speaker.PcmSpeaker.
	StockSound bool
	// StockCurve: with the Echo's own stock tuning in force (see
	// stockTuning), its FIR EQ, ParametricEQ and OutputTrim run ahead of the
	// 8 bands above (controller/em_eq.py's stock_curve). The MBCL runs
	// whatever this says. No effect without a loaded tuning.
	StockCurve bool
}

// DefaultParams mirrors the controller's defaults, so a device that has not
// yet had a config push sounds the way the controller would have made it.
func DefaultParams() Params {
	return Params{
		GuardEnabled:       true,
		GuardDb:            -30,
		LimiterEnabled:     true,
		LimiterThresholdDb: -1,
		LimiterReleaseMs:   150,
		// Always on for the speaker (#229); there is no key to turn it
		// off. chainForJack clears it while a plug is in.
		StockSound: true,
		StockCurve: true,
	}
}

// String is the one-line description em_eq.describe_chain gives, so device
// and controller logs read the same way.
func (p Params) String() string {
	eqs := "flat"
	if !isFlat(p.Bands, false) {
		parts := make([]string, NumBands)
		for i, b := range p.Bands {
			if b == 0 {
				parts[i] = "0"
			} else {
				parts[i] = fmt.Sprintf("%+g", b)
			}
		}
		eqs = strings.Join(parts, "/")
	}
	boost := "off"
	if p.Loudness {
		boost = "on"
	}
	guard := "off"
	if p.GuardEnabled {
		guard = fmt.Sprintf("%gdB", math.Min(p.GuardDb, 0))
	}
	lim := "off"
	if p.LimiterEnabled {
		lim = fmt.Sprintf("%gdB/%gms", math.Min(p.LimiterThresholdDb, 0), p.LimiterReleaseMs)
	}
	out := fmt.Sprintf("eq=%s speech_boost=%s guard=%s limiter=%s", eqs, boost, guard, lim)
	if p.StockSound {
		out += " stock_sound=on"
	}
	return out
}

// MaxPeriodFrames is the longest block the chain is asked to process: the
// speaker's loop period. The stock FIR is sized for it; shorter blocks work.
// internal/bindings/speaker pins its own period to this by test.
const MaxPeriodFrames = 2048

// Chain runs EQ → bass guard → limiter on stereo S16_LE periods.
//
// Order is em_eq's: the guard removes excursion the driver cannot deliver,
// THEN the limiter catches what is left. Limiting first would spend gain
// reduction on bass about to be thrown away.
//
// The processing is MONO: L and R are averaged, processed once and written
// back to both. The wire is mono and toStereo duplicates it, so the average
// is exact on everything this device plays; stereo output is not supported
// on this hardware (device/CLAUDE.md), and processing two identical channels
// would double the cost for nothing.
//
// Process runs on the ALSA write goroutine only. SetParams and SetActive may
// be called from anywhere; they take effect at the next period.
type Chain struct {
	fs float64

	active atomic.Bool // false: Process is a passthrough

	mu      sync.Mutex
	pending *Params // set by SetParams, taken by Process

	// volume is the device level the voicing's volume filters follow, set
	// by SetVolume; volApplied is the one they were last designed for.
	volume     atomic.Int64
	volApplied int64

	// Owned by the ALSA goroutine.
	params  Params
	voicing *board.Voicing // nil: StockSound is never in force
	eq      eq
	guard   *bassGuard
	corr    correction
	lim     *limiter
	idle    bool // state is all zero and input is silence
	running bool // active on the previous period

	// stock is the Echo's own playback tuning, read off its /system
	// (RadarTuning); nil when the board has none or it did not load. While
	// StockSound is in force it REPLACES the voicing's correction, upper
	// bands and limiter: it is what stock itself runs, read rather than
	// fitted. See stockTuning.
	stock *stockTuning
}

// stockTuning is a stock playback chain read off the device: the
// volume-banded FIR, ParametricEQ, MBCL (four band compressors and band
// limiters, then its full-band limiter) and OutputTrim, in AFE.cfg's order.
// It runs inside the stock-sound profile's order (volume ahead of the
// chain, ProcessWide), which is stock's own, under stock's mixer law: the
// speaker gives it no ceiling (HasStockTuning).
type stockTuning struct {
	firBands  [][]float64 // nil: no FIR in this tuning
	firBounds []float64   // each band's upper stock volume value
	fir       *eqFIR      // sized on first use
	peq       []biquad
	trimGain  float64
	mbcl      *radarMultiband // nil: no MBCL.cfg in this tuning
	lim       *stockLimiter
	scratch   []float64
	curveOn   bool // Params.StockCurve as of the last apply
	// The MBCL full-band limiter's own settings, for Describe.
	limThresholdDb, limReleaseMs float64
}

// New builds a chain at the given sample rate, inactive, with DefaultParams
// and biscuit's bass band and voicing.
func New(sampleRate int) *Chain {
	return NewFor(sampleRate, DefaultGuardBand, board.BiscuitVoicing())
}

// NewFor is New with the board's own bass band and voicing (nil for none).
func NewFor(sampleRate int, band GuardBand, v *board.Voicing) *Chain {
	return NewWithTuning(sampleRate, band, v, nil)
}

// NewWithTuning is NewFor with the device's own stock tuning (nil for none),
// which takes the voicing's place while StockSound is in force. A tuning
// with neither a FIR nor an MBCL is no tuning.
func NewWithTuning(sampleRate int, band GuardBand, v *board.Voicing, rt *RadarTuning) *Chain {
	fs := float64(sampleRate)
	c := &Chain{
		fs:      fs,
		voicing: v,
		eq:      eq{fs: fs},
		guard:   newBassGuard(fs, band),
		corr:    newCorrection(fs, v),
		lim:     newLimiter(fs),
		idle:    true,
	}
	if rt != nil && v != nil && (len(rt.FIRBands) > 0 || rt.MBCL != nil) {
		st := &stockTuning{trimGain: 1}
		if len(rt.FIRBands) > 0 {
			st.firBands, st.firBounds = rt.FIRBands, rt.FIRBounds
			st.peq = rt.peqBiquads(fs)
			st.trimGain = dbToGain(rt.TrimDb)
		}
		if m := rt.MBCL; m != nil {
			st.mbcl = newRadarMultiband(fs, m)
			st.lim = newStockLimiter(fs, m.FullBand.LimThresh, m.FullBand.LimRelease, m.FullBand.LimInVol)
			st.limThresholdDb, st.limReleaseMs = m.FullBand.LimThresh, m.FullBand.LimRelease
		}
		c.stock = st
	}
	c.volume.Store(127)
	c.volApplied = 127
	c.apply(DefaultParams())
	return c
}

// HasStockTuning reports whether the device's own stock tuning replaces the
// voicing under StockSound. Its volume law is stock's mixer law, 0dB at the
// top, rather than the voicing's ceiling.
func (c *Chain) HasStockTuning() bool { return c.stock != nil }

// tuned: the stock tuning is what runs this period.
func (c *Chain) tuned() bool { return c.stock != nil && c.params.StockSound }

// voiced: the fitted voicing is what runs this period.
func (c *Chain) voiced() bool { return c.stock == nil && c.params.StockSound }

// Describe is p.String() as this chain runs it: with the stock tuning in
// force, the limiter is MBCL's own at its own settings whatever p says, and
// the curve is named.
func (c *Chain) Describe(p Params) string {
	tuned := c.stock != nil && p.StockSound && c.voicing != nil
	if tuned && c.stock.mbcl != nil {
		p.LimiterThresholdDb, p.LimiterReleaseMs = c.stock.limThresholdDb, c.stock.limReleaseMs
	}
	s := p.String()
	if tuned {
		on := "off"
		if p.StockCurve && c.stock.firBands != nil {
			on = "on"
		}
		s += " stock_tuning=on stock_curve=" + on
	}
	return s
}

// SetActive turns processing on or off. Off is a passthrough, which is what
// a device must do while its controller is still processing the audio itself:
// the chain applied twice is a doubled EQ curve and a second limiter.
func (c *Chain) SetActive(on bool) { c.active.Store(on) }

// Active reports whether the chain is processing.
func (c *Chain) Active() bool { return c.active.Load() }

// SetParams queues a new configuration for the next period. Everything is
// updated in place — filter states, the limiter's delay line and its gain
// carry across — so a change mid-song does not click.
func (c *Chain) SetParams(p Params) {
	c.mu.Lock()
	c.pending = &p
	c.mu.Unlock()
}

func (c *Chain) apply(p Params) {
	// Cleared rather than ignored, so everything downstream (the speaker's
	// volume law, the log line) reads what is in force.
	p.StockSound = p.StockSound && c.voicing != nil
	c.params = p
	c.eq.set(p.Bands, p.Loudness)
	// The voicing was tuned with the guard on, and drives the chain past
	// full scale near the top, so the profile does not let it be turned off.
	c.guard.enabled = p.GuardEnabled || p.StockSound
	c.guard.floorDb = math.Min(p.GuardDb, 0)
	if p.StockSound {
		c.guard.floorDb = math.Min(c.guard.floorDb, c.voicing.BassFloorDb)
	}
	c.lim.enabled = p.LimiterEnabled
	// The profile's extra filters start cold when it turns on (one phase
	// step at a rare config change), and are dropped when it turns off so
	// the default path stays bit-exact.
	switch {
	case p.StockSound && c.stock == nil && c.guard.upper == nil:
		c.guard.upper = newUpperBands(c.fs, c.voicing)
		c.guard.hold = newHoldMin(c.voicing.BassHoldMs, c.fs)
		c.corr.reset()
	case !p.StockSound || c.stock != nil:
		c.guard.upper = nil
		c.guard.hold = nil
	}
	c.lim.setParams(p.LimiterThresholdDb, p.LimiterReleaseMs, c.fs)
	if st := c.stock; st != nil {
		// MBCL and its limiter are stock's, at stock's own settings; the
		// guard and limiter switches still turn them off, and the guard's
		// floor bounds band 1, as before the voicing existed. The FIR is
		// switched here and sized in ProcessWide, the only place that knows
		// the period length.
		if st.mbcl != nil {
			st.mbcl.setEnabled(p.GuardEnabled)
			st.mbcl.setFloorDb(math.Min(p.GuardDb, 0))
			st.lim.setEnabled(p.LimiterEnabled)
		}
		on := p.StockCurve && st.firBands != nil
		if on != st.curveOn {
			st.curveOn = on
			st.fir = nil
			for i := range st.peq {
				st.peq[i].reset()
			}
		}
	}
}

// SetVolume tells the chain the device level (0..127), for a voicing whose
// EQ follows the volume. It takes effect at the next period.
func (c *Chain) SetVolume(level int) { c.volume.Store(int64(level)) }

// takePending applies a queued SetParams. Returns the params that are now in
// force and whether they changed.
func (c *Chain) takePending() (Params, bool) {
	c.mu.Lock()
	p := c.pending
	c.pending = nil
	c.mu.Unlock()
	if p == nil {
		return c.params, false
	}
	prev := c.params
	c.apply(*p)
	changed := c.params != prev
	return c.params, changed
}

// Idle reports whether processing a silent period would return silence
// unchanged, so the caller can skip it. True while inactive.
func (c *Chain) Idle() bool {
	return !c.active.Load() || c.idle
}

// Process runs the chain over one stereo S16_LE period, IN PLACE, and returns
// the same buffer. The caller must own buf — never pass a shared silence
// buffer.
//
// The first return is non-nil only when a queued SetParams changed the chain
// on this period, so the caller can log what the audio is now going through.
func (c *Chain) Process(buf []byte) (applied *Params) {
	applied, active := c.beginProcess()
	if !active {
		return applied
	}

	frames := len(buf) / 4
	silentIn, silentOut := true, true
	for i := 0; i < frames; i++ {
		off := i * 4
		l := int16(uint16(buf[off]) | uint16(buf[off+1])<<8)
		r := int16(uint16(buf[off+2]) | uint16(buf[off+3])<<8)
		if l != 0 || r != 0 {
			silentIn = false
		}
		x := (float64(l) + float64(r)) / 2

		x = c.eq.step(x)
		if c.voiced() {
			x = c.corr.step(x)
		}
		x = c.guard.step(x)
		x = c.lim.step(x)

		// Backstop, then truncation toward zero — np.clip(...).astype(int16)
		// in the reference.
		if x > ceiling {
			x = ceiling
		} else if x < -fullScale {
			x = -fullScale
		}
		s := int16(x)
		if s != 0 {
			silentOut = false
		}
		lo, hi := byte(uint16(s)), byte(uint16(s)>>8)
		buf[off], buf[off+1], buf[off+2], buf[off+3] = lo, hi, lo, hi
	}

	// A silent period that came out silent means every filter tail has
	// decayed below one LSB. Zero the state and stop processing silence
	// until audio returns — otherwise the chain runs flat out on an idle
	// speaker, forever. The reset moves the output by less than one LSB.
	if silentIn && silentOut {
		c.reset()
		c.idle = true
	} else {
		c.idle = false
	}
	return applied
}

// ProcessFloat is Process for a wide-precision interleaved stereo period.
// fullScales contains one full-scale multiplier per frame. Samples are
// normalised into the chain's ordinary S16 signal domain and returned to the
// wide domain afterward. Keeping the chain's state normalised makes switching
// between ordinary and boosted periods seamless, while the caller retains
// headroom until the later master-volume stage.
func (c *Chain) ProcessFloat(buf []float64, fullScales []float64) (applied *Params) {
	applied, active := c.beginProcess()
	if !active {
		return applied
	}

	frames := len(buf) / 2
	silentIn, silentOut := true, true
	for i := 0; i < frames; i++ {
		l, r := buf[i*2], buf[i*2+1]
		if l != 0 || r != 0 {
			silentIn = false
		}
		scale := 1.0
		if i < len(fullScales) && fullScales[i] > 0 {
			scale = fullScales[i]
		}
		x := (l + r) / (2 * scale)
		x = c.eq.step(x)
		if c.voiced() {
			x = c.corr.step(x)
		}
		x = c.guard.step(x)
		x = c.lim.step(x)
		if x > ceiling {
			x = ceiling
		} else if x < -fullScale {
			x = -fullScale
		}
		x *= scale
		if math.Abs(x) >= 1 {
			silentOut = false
		}
		buf[i*2], buf[i*2+1] = x, x
	}
	if silentIn && silentOut {
		c.reset()
		c.idle = true
	} else {
		c.idle = false
	}
	return applied
}

// ProcessWide runs the chain over a stereo period at its TRUE level, which
// may exceed full scale, and writes S16 to out. It is the stock-sound
// profile's entry point: the volume is applied BEFORE the chain there, as on
// stock, and near the top it puts the signal over full scale on purpose for the
// compressors and limiter to bring back. Only the chain's output is clipped.
// len(out) must be 2*len(in) bytes (stereo S16 for stereo float).
func (c *Chain) ProcessWide(in []float64, out []byte) (applied *Params) {
	applied, active := c.beginProcess()
	frames := min(len(in)/2, len(out)/4)
	tuned := active && c.tuned()
	var mono []float64
	if tuned {
		mono = c.stock.firstStage(in, frames, c.volApplied)
	}
	silentIn, silentOut := true, true
	for i := 0; i < frames; i++ {
		l, r := in[i*2], in[i*2+1]
		if l != 0 || r != 0 {
			silentIn = false
		}
		x := (l + r) / 2
		switch {
		case tuned:
			x = c.stock.rest(mono[i], &c.eq)
		case active:
			x = c.eq.step(x)
			if c.voiced() {
				x = c.corr.step(x)
			}
			x = c.guard.step(x)
			x = c.lim.step(x)
		}
		if x > ceiling {
			x = ceiling
		} else if x < -fullScale {
			x = -fullScale
		}
		if tuned {
			// Rounded, as em_eq._to_int16's np.rint: at the lowest levels
			// the whole signal is a few LSB, and truncation toward zero
			// costs ~6dB of signal-to-error.
			x = math.RoundToEven(x)
		}
		s := int16(x)
		if s != 0 {
			silentOut = false
		}
		lo, hi := byte(uint16(s)), byte(uint16(s)>>8)
		off := i * 4
		out[off], out[off+1], out[off+2], out[off+3] = lo, hi, lo, hi
	}
	if !active {
		return applied
	}
	if silentIn && silentOut {
		c.reset()
		c.idle = true
	} else {
		c.idle = false
	}
	return applied
}

func (c *Chain) beginProcess() (applied *Params, active bool) {
	if p, changed := c.takePending(); changed {
		applied = &p
	}
	if lv := c.volume.Load(); lv != c.volApplied {
		c.volApplied = lv
		c.corr.setVolume(float64(min(max(lv, 0), 127)) * 100 / 127)
	}
	active = c.active.Load()
	if active != c.running {
		// Entering or leaving: the chain's state belongs to audio it last
		// saw, which is not the audio arriving now. Start clean.
		c.reset()
		c.running = active
	}
	return applied, active
}

func (c *Chain) reset() {
	c.eq.reset()
	c.corr.reset()
	c.guard.reset()
	c.lim.reset()
	if st := c.stock; st != nil {
		st.reset()
	}
	c.idle = true
}

// firstStage is the FIR over the whole period, the one block stage: the
// stereo input averaged to mono, then the curve stock plays at this volume
// (its MUSIC VOLUME VALUE, from the device level by stock's mixer law). A
// band change crossfades across the period. Returns the mono period.
func (st *stockTuning) firstStage(in []float64, frames int, level int64) []float64 {
	if len(st.scratch) < frames {
		st.scratch = make([]float64, max(frames, MaxPeriodFrames))
	}
	x := st.scratch[:frames]
	for i := range x {
		x[i] = (in[i*2] + in[i*2+1]) / 2
	}
	if !st.curveOn {
		return x
	}
	band := radarEQBand(levelGain(level), st.firBounds)
	if st.fir == nil {
		// Sized for the longest period whatever this one is.
		st.fir = newEQFIRBands(st.firBands, max(frames, MaxPeriodFrames))
		st.fir.startOn(band)
	}
	st.fir.setBand(band)
	copy(x, st.fir.process(x))
	return x
}

// rest is the per-sample remainder: ParametricEQ (with the curve), the
// user's bands, MBCL, its full-band limiter, then OutputTrim — AFE.cfg's
// order, with the user's EQ layered where em_eq puts it.
func (st *stockTuning) rest(v float64, e *eq) float64 {
	if st.curveOn {
		for j := range st.peq {
			v = st.peq[j].step(v)
		}
	}
	v = e.step(v)
	if st.mbcl != nil {
		v = st.mbcl.step(v)
		v = st.lim.step(v)
	}
	if st.curveOn {
		v *= st.trimGain
	}
	return v
}

func (st *stockTuning) reset() {
	if st.fir != nil {
		st.fir.reset()
	}
	for i := range st.peq {
		st.peq[i].reset()
	}
	if st.mbcl != nil {
		st.mbcl.reset()
		st.lim.reset()
	}
}

// levelGain is a device level's gain under stock's mixer law (0.5dB per
// level, 0dB at 127), the law the stock tuning plays at.
func levelGain(level int64) float64 {
	if level <= 0 {
		return 0
	}
	return math.Pow(10, float64(min(level, 127)-127)/40)
}

// Stats is the chain's instrumentation: the WORK done, as against Params,
// which is what it was set to. A stage that is on and reports 0.00dB never
// engaged, which is a different fault from one that is off.
type Stats struct {
	GuardReductionDb   float64
	LimiterReductionDb float64
	Clipped            uint64 // must stay 0 while limiting
	ClippedBypassed    uint64
}

// TakeStats returns and clears the maximum reductions since the last call.
// ALSA goroutine only.
func (c *Chain) TakeStats() Stats {
	s := Stats{
		GuardReductionDb:   c.guard.maxReductionDb,
		LimiterReductionDb: c.lim.maxReductionDb,
		Clipped:            c.lim.clipped,
		ClippedBypassed:    c.lim.clippedBypassed,
	}
	c.guard.maxReductionDb, c.lim.maxReductionDb = 0, 0
	if st := c.stock; st != nil && st.mbcl != nil {
		// Both ran this window only across a mode change; report the worse.
		s.GuardReductionDb = max(s.GuardReductionDb, st.mbcl.takeMaxReductionDb())
		red, clipped, bypassed := st.lim.takeStats()
		s.LimiterReductionDb = max(s.LimiterReductionDb, red)
		s.Clipped += clipped
		s.ClippedBypassed += bypassed
	}
	return s
}
