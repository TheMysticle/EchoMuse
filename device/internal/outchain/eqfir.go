package outchain

import (
	"math"
)

// Radar's stock FIR is several curves, chosen by volume: AFE.cfg's
// "Equalizer FIR" lists EQ_<n>.cfg files against "Volume Boundary". On the
// stock Echo 2 they are a loudness compensation (more bass at low volume),
// not one curve at several gains, which is what biscuit's EQ files are. Read
// from the Echo at start-up — see RadarTuning.

// stockMixerLevels is stock's attenuation for each 0-100 music volume value,
// read out of /system/bin/mixer (Mixer_AlgoRampGain): a level in our own law,
// 0.5dB per step with 127 = 0dB. From value 11 up it is value + 27. It is how
// a volume is turned back into the value AFE.cfg's "Volume Boundary" is
// written in — em_eq.STOCK_MIXER_LEVELS, which this mirrors. (Android's
// speaker volume curve, used before, is not on stock's Alexa audio path.)
var stockMixerLevels = func() [101]float64 {
	var t [101]float64
	copy(t[:], []float64{0, 3, 7, 11, 17, 20, 27, 30, 32, 35, 36})
	for v := 11; v <= 100; v++ {
		t[v] = float64(v + 27)
	}
	return t
}()

// stockVolumeValue is stock's 0-100 music volume value for a linear volume
// gain: the highest value whose mixer level is at or below ours —
// em_eq.stock_volume_value. A gain is always one of the device's own levels,
// so the level comes back exactly; the 1e-6 absorbs the round trip.
func stockVolumeValue(gain float64) int {
	if gain <= 0 {
		return 0
	}
	level := 127 + 40*math.Log10(gain)
	value := 0
	for v, lv := range stockMixerLevels {
		if lv <= level+1e-6 {
			value = v
		}
	}
	return value
}

// radarEQBand is which banded FIR stock plays at this volume gain: the
// first whose boundary is at or above the volume value (libasp's own rule)
// — em_eq.radar_eq_band.
func radarEQBand(gain float64, boundaries []float64) int {
	value := float64(stockVolumeValue(gain))
	for i, b := range boundaries {
		if value <= b {
			return i
		}
	}
	return len(boundaries) - 1
}

// eqFIR runs Radar's stock speaker EQ — a long (2048-tap) FIR, not the
// biquad shape the rest of this package's eq type uses — via FFT-based
// overlap-save. See controller/em_eq.py's _OverlapSaveFIR, which this is
// held bit-exact to (testdata/gen_vectors.py + vectors.json).
//
// Block size, unlike the Python reference: the FFT is sized once for the
// longest block (periodSamples), and process takes any block from one sample
// up to that. Overlap-save is exact for any block that fits the FFT: the new
// samples are appended to the carried history, convolved, and the valid part
// of the product is exactly those samples' outputs. The speaker hands it a
// whole period, or a shorter block when a low-latency stream has the speaker to
// itself; a block LONGER than it was sized for panics, which is the correct
// failure for a bug in the caller rather than silently computing a wrong
// answer. Tests prove the general overlap-save MATH via fft_test.go, and that
// different block sizes give the same output (TestEQFIRIsTheSameInHalfBlocks).
//
// The input is real, so each period is transformed with a complex FFT of
// HALF the block size and the usual even/odd split (realFFT), and only the
// half spectrum is multiplied: about half the work of a full complex FFT of
// a real signal, with every twiddle precomputed. The result is the same
// convolution to rounding.
type eqFIR struct {
	hs       [][]complex128 // per band: the taps' half spectrum, bins 0..n/2
	band     int
	fadeFrom int // band to crossfade FROM on the next period, or -1
	m        int // filter length (taps)
	period   int // the longest block (new samples per call) this was sized for
	rf       *realFFT
	overlap  []float64 // last m-1 raw input samples carried across periods

	// Reused across every call, so a period allocates nothing: this runs
	// every ~42.7ms on hardware the rest of this package is visibly tuned
	// to keep allocation-free.
	in      []float64    // the block: overlap, then the period, then zeros
	spec    []complex128 // the block's half spectrum
	prod    []complex128 // spectrum times a band's
	time    []float64    // a product back in time
	out     []float64
	fadeOut []float64
}

// newEQFIR builds an overlap-save FIR for taps, sized for exactly
// periodSamples new samples per call. Returns nil if taps is empty (no
// data loaded) — callers must treat a nil *eqFIR as "run nothing",
// mirroring the controller's fallback without a Radar tuning.
func newEQFIR(taps []float64, periodSamples int) *eqFIR {
	if len(taps) == 0 {
		return nil
	}
	return newEQFIRBands([][]float64{taps}, periodSamples)
}

// newEQFIRBands is newEQFIR over several filters of equal length, starting
// on band 0, switched with setBand. Nil for no bands or unequal lengths.
func newEQFIRBands(bands [][]float64, periodSamples int) *eqFIR {
	if len(bands) == 0 || len(bands[0]) == 0 {
		return nil
	}
	m := len(bands[0])
	n := nextPow2(m - 1 + periodSamples)
	if n < 4 {
		n = 4
	}
	rf := newRealFFT(n)

	hs := make([][]complex128, len(bands))
	padded := make([]float64, n)
	for b, taps := range bands {
		if len(taps) != m {
			return nil
		}
		for i := range padded {
			padded[i] = 0
		}
		copy(padded, taps)
		hs[b] = make([]complex128, n/2+1)
		rf.forward(padded, hs[b])
	}

	return &eqFIR{
		hs:       hs,
		fadeFrom: -1,
		m:        m,
		period:   periodSamples,
		rf:       rf,
		overlap:  make([]float64, m-1),
		in:       make([]float64, n),
		spec:     make([]complex128, n/2+1),
		prod:     make([]complex128, n/2+1),
		time:     make([]float64, n),
		out:      make([]float64, periodSamples),
		fadeOut:  make([]float64, periodSamples),
	}
}

// process filters len(x) samples, 1 to f.period, returning a prefix of
// f.out — reused across calls, so the result must be consumed before the next
// call to process (true of every call site in this package: Chain.Process
// reads it back within the same period it was produced in, never retains it
// across periods). Panics if len(x) is 0 or more than f.period.
func (f *eqFIR) process(x []float64) []float64 {
	n := len(x)
	if n == 0 || n > f.period {
		panic("eqFIR.process: called with a longer block than newEQFIR was sized for")
	}

	in := f.in
	copy(in, f.overlap)
	copy(in[f.m-1:], x)
	for i := f.m - 1 + n; i < len(in); i++ {
		in[i] = 0
	}
	f.rf.forward(in, f.spec)

	out := f.out[:n]
	if f.fadeFrom >= 0 {
		f.convolve(f.hs[f.fadeFrom], f.fadeOut[:n])
	}
	f.convolve(f.hs[f.band], out)
	if f.fadeFrom >= 0 {
		// Linear crossfade across the period from the old band's output to
		// the new one's — both filtered the same input history, so this is
		// a change of curve, faded so it does not land as a step.
		fn := float64(n)
		for i := range out {
			w := float64(i+1) / fn
			out[i] = f.fadeOut[i]*(1-w) + out[i]*w
		}
		f.fadeFrom = -1
	}

	// Carry the last m-1 RAW input samples for the next call — history of
	// the signal, not of the filtered output. They are the tail of the
	// block just assembled.
	copy(f.overlap, in[n:n+f.m-1])
	return out
}

// convolve multiplies the block's spectrum by h and writes the valid part
// of the circular convolution — the period's new samples — to out.
func (f *eqFIR) convolve(h []complex128, out []float64) {
	for k, v := range f.spec {
		f.prod[k] = v * h[k]
	}
	f.rf.inverse(f.prod, f.time)
	copy(out, f.time[f.m-1:f.m-1+len(out)])
}

// setBand switches filter; the next process crossfades into it. Setting the
// band it is already on does nothing.
func (f *eqFIR) setBand(b int) {
	if b == f.band || b < 0 || b >= len(f.hs) {
		return
	}
	f.fadeFrom = f.band
	f.band = b
}

// startOn puts the filter on band b with no crossfade — for its first
// period, where there is no previous curve to fade from.
func (f *eqFIR) startOn(b int) {
	if b < 0 || b >= len(f.hs) {
		return
	}
	f.band, f.fadeFrom = b, -1
}

func (f *eqFIR) reset() {
	for i := range f.overlap {
		f.overlap[i] = 0
	}
}

// StockVolumeLevel is stock's device level for its 0-100 music volume value
// (stockMixerLevels): what a server's volume means on Radar, as the
// controller's em_volume.ha_volume_to_device reads HA's slider.
func StockVolumeLevel(value int) int {
	return int(stockMixerLevels[min(max(value, 0), 100)])
}

// StockVolumeValue is the inverse: the highest value whose level is at or
// below this one. The table is strictly increasing, so a value round-trips
// exactly, which a server needs or it and the device correct each other
// forever.
func StockVolumeValue(level int) int {
	value := 0
	for v, lv := range stockMixerLevels {
		if int(lv) <= level {
			value = v
		}
	}
	return value
}
