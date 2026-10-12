package beamformer

// ARA: an adaptive interference canceller after the beams.
//
// A fixed beam can only reject a source by where its pattern is low. A TV or a
// speaker on a shelf sits at one place, and the beams that point away from the
// talker hear it with the talker barely present. So each beam's output is
// cleaned with a filtered copy of two such beams: the target beam's
// interference is predicted from them per frequency band and subtracted, and
// the filter is learnt, not designed, so it finds the actual source in the
// actual room, reverberation included.
//
// Radar's stock AFE does the same after its beams (AFE.cfg "ASR ARA V2"): for
// target beam i the references are beams i+3 and i+5, 135 degrees either side
// of the far side of the ring (the table [3,5],[4,6],...,[2,4]), the tail is
// 96ms, the error band 400-2500Hz, and adaptation is controlled by speech
// probabilities so it does not learn the talker. This is our own: the same
// shape, our own filters, and our own adaptation control (below).
//
// It works in a 256-point, 50%-overlap short-time spectrum (sine window,
// perfect reconstruction), the filter bank's 62.5Hz bins and 8ms hop, with a
// complex filter per bin per reference over araTaps frames, normalised per bin
// so a loud band does not slow a quiet one. The spectra are all calculated
// once per hop for all eight beams, and each beam is the reference of two
// others.
//
// The failure to design against is the talker leaking into a reference and
// being subtracted from itself. Adaptation is therefore allowed only while the
// references carry more energy than the target beam in the speech band (the
// interferer dominates: the talker is not in the target beam), held off for a
// while after the target beam dominates, and the weights leak toward zero
// while adapting so nothing can run away. The suppression per bin is also
// limited (araMinGain), so no bin is ever cancelled outright.

import (
	"math"
	"math/cmplx"
)

const (
	araFFT  = 256
	araHop  = 128
	araBins = araFFT/2 + 1
	araRefs = 2

	// araTaps frames of reference history per bin: 6 x 8ms = 48ms. Stock's tail
	// is 96ms; the references here are neighbours of the target's own beam
	// system, so the relation between beams is short and 48ms is the compromise
	// with the cost. It is a constant so it can be tried.
	araTaps = 6

	// ARADelay is the fixed delay the stage adds, in samples: the 128 a frame
	// of 50% overlap needs plus the remainder of a hop that has not filled yet,
	// rounded up so it never varies with how the input is chopped into calls.
	ARADelay = 256

	// The adaptation band, in bins of 62.5Hz: 375-2500Hz, stock's 400-2500.
	araBandLo, araBandHi = 6, 40

	// araKMax is the top bin cancelled, 6kHz: above the speech the wake word
	// scores, where the beams are close to a plain sum, and 25% of the cost.
	araKMax = 96
)

// Tunables, variables so tools/bf_design can sweep them.
var (
	// ARAMu is the step size while the filters are young (ARATrainFrames
	// adapting frames) and ARAMuSteady after; stock uses 0.75 and 0.5.
	ARAMu, ARAMuSteady = 0.2, 0.05
	ARATrainFrames     = 500 // adapting frames, 4s
	// ARARatio: adapt only while the references' band power is above this
	// multiple of the target beam's.
	ARARatio = 0.5
	// ARAHold is how many frames after the target beam dominated before
	// adaptation resumes: 12 x 8ms, stock's beam-group hold.
	ARAHold = 12
	// ARALeak is the fraction the weights shrink by on each adapting frame.
	ARALeak = 2e-4
	// ARAUnexplained: the filters are held while the residual is this large a
	// fraction of the target beam's band power AND more than twice what it has
	// been while they were learning well, which is energy the references
	// cannot explain: the talker. ARARelearn frames of that and it is taken as
	// the new normal, so a moved interferer is learnt again (3.2s at 8ms).
	ARAUnexplained = 0.35
	ARARelearn     = 400
	// ARAGate chooses what decides whether a beam's filters learn. ARAGateOurs
	// is the original control (the references louder than the target, held off
	// after it dominated, frozen on energy the filters cannot explain).
	// ARAGateStock is stock's, decoded from libasp: a beam's own output SNR
	// (speech-band power over a minimum-statistics floor) scales the step by a
	// logistic gate (about 1 below SNR 3, half at 10, 5% at 20), and when ANY
	// beam is over the half-way point nothing learns. ARAGateBoth needs both to
	// agree. Default is ours until the other has been measured against it.
	ARAGate = ARAGateOurs
	// ARAMinGain limits the suppression per bin: the output is never below
	// this fraction of the target beam's own magnitude (0.1 is -20dB).
	ARAMinGain = 0.1
)

const (
	ARAGateOurs = iota
	ARAGateStock
	ARAGateBoth
)

// araGateTable is stock's step-size gate as a function of the index it looks
// the SNR up by (128 x min(1, 256/(0.3*snr+1)) entries): a logistic centred at
// 61.4 with scale 8.5, fitted to the 128 values in libasp to within 5e-4. Fitted
// rather than copied: it is a smooth curve, and a formula is all that is needed.
var araGateTable = func() (t [128]float64) {
	for i := range t {
		t[i] = 1 / (1 + math.Exp(-(float64(i)-61.43)/8.524))
	}
	return
}()

// araGate is the gate for a smoothed SNR: stock's index is 128 * 2 /
// (0.3*snr + 1) (its constants 1.0, 2.0 and 0.3), clamped to the table.
func araGate(snr float64) float64 {
	idx := int(256 / (0.3*snr + 1))
	if idx > 127 {
		idx = 127
	}
	if idx < 0 {
		idx = 0
	}
	return araGateTable[idx]
}

// araRefs returns target beam b's two reference beams of n on a ring: the two
// 135 degrees away, as in stock's table.
func araRefBeams(b, n int) [araRefs]int {
	return [araRefs]int{(b + 3*n/8) % n, (b + 5*n/8) % n}
}

var (
	araTw, araTwInv [araFFT / 2]complex64
	araRev          [araFFT]uint16
	araWin          [araFFT]float32
)

func init() {
	for k := range araTw {
		a := -2 * math.Pi * float64(k) / araFFT
		araTw[k] = complex64(cmplx.Rect(1, a))
		araTwInv[k] = complex64(cmplx.Rect(1, -a))
	}
	bits := 8
	for i := range araRev {
		var r uint16
		for b := 0; b < bits; b++ {
			if i&(1<<b) != 0 {
				r |= 1 << (bits - 1 - b)
			}
		}
		araRev[i] = r
	}
	for i := range araWin {
		araWin[i] = float32(math.Sin(math.Pi * (float64(i) + 0.5) / araFFT))
	}
}

// araFFTc transforms x in place (inverse scales by 1/N).
func araFFTc(x []complex64, inverse bool) {
	for i := 0; i < araFFT; i++ {
		if j := int(araRev[i]); i < j {
			x[i], x[j] = x[j], x[i]
		}
	}
	tw := &araTw
	if inverse {
		tw = &araTwInv
	}
	for size := 2; size <= araFFT; size <<= 1 {
		half, step := size/2, araFFT/size
		for start := 0; start < araFFT; start += size {
			for k := 0; k < half; k++ {
				a := x[start+k]
				b := x[start+k+half] * tw[k*step]
				x[start+k], x[start+k+half] = a+b, a-b
			}
		}
	}
	if inverse {
		s := complex(float32(1)/araFFT, 0)
		for i := range x[:araFFT] {
			x[i] *= s
		}
	}
}

// ARA is the canceller for n beams on a ring (n=8 as in stock).
type ARA struct {
	n  int
	on bool

	// Microphone mode (ProcessMics): the mics' last araFFT samples, the hop
	// being collected, their spectra, and the weights that turn them into beams.
	mode        bool
	bufM, pendM [][]float32
	ms          [][araBins]complex64
	sd          *SDWeights

	buf  [][]float32 // each beam's last araFFT input samples
	pend [][]float32 // the hop being collected
	np   int
	acc  [][]float32 // overlap-add accumulator
	fin  [][]float32 // finished samples not yet handed out

	// h[bin][beam] is that bin's spectrum over the last araTaps frames, newest
	// first: contiguous, so a bin's references and its weights sit together.
	h    [araBins][8][araTaps]complex64
	cur  [][araBins]complex64                   // this frame's spectra
	w    [][araRefs][araBins][araTaps]complex64 // [target][ref][bin][age]
	spec [][araBins]complex64                   // output spectra
	fft  []complex64

	// Adaptation control and what to say about it, per target beam.
	pxS, prS [8]float64
	hold     [8]int
	adapting [8]bool
	resS     [8]float64 // residual/target power while learning well
	resLast  [8]float64 // the last frame's
	frozen   [8]int     // frames held for unexplained energy
	trained  [8]int
	cut      [8]float64 // smoothed band suppression, dB

	// Stock's adaptation gate: each beam's output SNR (see ARAGate).
	gsnr      [8]beamSNR
	gsm       [8]float64 // the SNR smoothed 0.75/0.25 as stock does
	gframes   [8]int
	gate      [8]float64 // the gate from the last frame
	freezeAll bool       // some beam's gate was under one half
	gmode     int        // ARAGateOurs, ARAGateStock or ARAGateBoth
}

// NewARA returns a canceller for n beams, off: until enabled it passes the
// beams through with the same delay, so switching it does not move the audio.
func NewARA(n int) *ARA {
	// The reference table and the per-beam state are for eight beams on a
	// ring, as in stock. Anything else needs its own table, not a silent index.
	if n != 8 {
		panic("beamformer: ARA is built for 8 beams on a ring")
	}
	a := &ARA{n: n, gmode: ARAGate}
	a.buf = make([][]float32, n)
	a.pend = make([][]float32, n)
	a.acc = make([][]float32, n)
	a.fin = make([][]float32, n)
	for b := range a.buf {
		a.buf[b] = make([]float32, araFFT)
		a.pend[b] = make([]float32, araHop)
		a.acc[b] = make([]float32, araFFT)
		a.fin[b] = make([]float32, araHop) // 128 zeros: the stream starts delayed
	}
	for b := range a.resS {
		a.resS[b], a.resLast[b] = 1, 1
		a.gate[b], a.gsm[b] = 1, 1
	}
	a.cur = make([][araBins]complex64, n)
	a.w = make([][araRefs][araBins][araTaps]complex64, n)
	a.spec = make([][araBins]complex64, n)
	a.fft = make([]complex64, araFFT)
	return a
}

// SetEnabled turns the cancelling on or off. The spectra are computed either
// way, so the output timing is the same and there is nothing to ramp.
func (a *ARA) SetEnabled(on bool) { a.on = on }

// SetGate chooses what lets the filters learn (ARAGateOurs, ARAGateStock,
// ARAGateBoth).
func (a *ARA) SetGate(mode int) { a.gmode = mode }

// Enabled reports whether cancelling is on.
func (a *ARA) Enabled() bool { return a.on }

// Adapting is whether target beam b's filters learnt in the last frame.
func (a *ARA) Adapting(b int) bool { return a.adapting[b] }

// CutDB is the smoothed suppression ARA gave beam b in the speech band: how
// far its output is below the beam without it.
func (a *ARA) CutDB(b int) float64 { return a.cut[b] }

// Process takes n samples of every beam and returns the same amount, delayed by
// ARADelay, written into out (each at least that long).
func (a *ARA) Process(in, out [][]float32) {
	a.mode = false
	a.feed(a.pend, in)
	a.emit(out, len(in[0]))
}

// ProcessMics is Process for microphones: it forms the beams itself, in the
// spectral domain, from the weights (one complex weight per mic per bin), then
// cancels and returns the beams, delayed by ARADelay. This is how the firmware
// uses the stage; forming the beams from the spectra the stage already takes
// costs a few complex multiplies per bin where time-domain filters cost a
// multiply-add per tap per sample.
func (a *ARA) ProcessMics(mics [][]float32, w *SDWeights, out [][]float32) {
	if len(a.pendM) != len(mics) {
		a.pendM = make([][]float32, len(mics))
		a.bufM = make([][]float32, len(mics))
		for m := range mics {
			a.pendM[m] = make([]float32, araHop)
			a.bufM[m] = make([]float32, araFFT)
		}
		a.ms = make([][araBins]complex64, len(mics))
	}
	a.mode, a.sd = true, w
	a.feed(a.pendM, mics)
	a.emit(out, len(mics[0]))
}

// feed collects input a hop at a time and runs a frame for each hop.
func (a *ARA) feed(pend, in [][]float32) {
	n := len(in[0])
	for i := 0; i < n; {
		// Fill the hop being collected a run at a time, not a sample at a time.
		take := min(araHop-a.np, n-i)
		for c := range pend {
			copy(pend[c][a.np:a.np+take], in[c][i:i+take])
		}
		i += take
		if a.np += take; a.np == araHop {
			a.np = 0
			a.frame()
		}
	}
}

// emit hands out n finished samples of every beam.
func (a *ARA) emit(out [][]float32, n int) {
	for b := 0; b < a.n; b++ {
		// ARADelay is chosen so that this is always satisfied; if it were
		// not, the stage would be handing out silence, so say nothing and pad.
		copy(out[b][:n], a.fin[b])
		if len(a.fin[b]) >= n {
			a.fin[b] = a.fin[b][:copy(a.fin[b], a.fin[b][n:])]
		} else {
			clear(out[b][len(a.fin[b]):n])
			a.fin[b] = a.fin[b][:0]
		}
	}
}

// spectra pairs two real signals into one complex transform: z = a + jb, so
// A_k = (Z_k + conj Z_{N-k})/2 and B_k = (Z_k - conj Z_{N-k})/(2j).
func (a *ARA) spectra(x1, x2 []float32, s1, s2 *[araBins]complex64) {
	for i := 0; i < araFFT; i++ {
		re := x1[i] * araWin[i]
		var im float32
		if x2 != nil {
			im = x2[i] * araWin[i]
		}
		a.fft[i] = complex(re, im)
	}
	araFFTc(a.fft, false)
	for k := 0; k < araBins; k++ {
		z, zc := a.fft[k], a.fft[(araFFT-k)%araFFT]
		zc = complex(real(zc), -imag(zc))
		s1[k] = (z + zc) * 0.5
		if s2 != nil {
			d := z - zc // = 2j * B
			s2[k] = complex(imag(d)*0.5, -real(d)*0.5)
		}
	}
}

// frame processes one hop: spectra, the cancelling, and the overlap-add back
// to samples.
func (a *ARA) frame() {
	n := a.n
	cur := a.cur

	if a.mode {
		// Microphones in: their spectra, then the beams as weighted sums.
		nm := len(a.pendM)
		for m := 0; m < nm; m++ {
			copy(a.bufM[m][:araHop], a.bufM[m][araHop:])
			copy(a.bufM[m][araHop:], a.pendM[m])
		}
		for m := 0; m < nm; m += 2 {
			if m+1 < nm {
				a.spectra(a.bufM[m], a.bufM[m+1], &a.ms[m], &a.ms[m+1])
			} else {
				a.spectra(a.bufM[m], nil, &a.ms[m], nil)
			}
		}
		for b := 0; b < n; b++ {
			cur[b][0], cur[b][araBins-1] = 0, 0
			for k := 1; k < araBins-1; k++ {
				var sum complex64
				for m := 0; m < nm; m++ {
					sum += a.sd.W[b][m][k] * a.ms[m][k]
				}
				cur[b][k] = sum
			}
		}
	} else {
		// Beams in: their spectra, two real beams to a complex transform.
		for b := 0; b < n; b++ {
			copy(a.buf[b][:araHop], a.buf[b][araHop:])
			copy(a.buf[b][araHop:], a.pend[b])
		}
		for b := 0; b < n; b += 2 {
			a.spectra(a.buf[b], a.buf[b+1], &cur[b], &cur[b+1])
		}
	}

	// This frame becomes age 0 of every bin's history.
	for k := 1; k < araBins-1; k++ {
		for b := 0; b < n; b++ {
			hv := &a.h[k][b]
			copy(hv[1:], hv[:araTaps-1])
			hv[0] = cur[b][k]
		}
	}

	// Cancelling. Stock freezes every beam's learning as soon as one beam's
	// gate is under a half (not an average: the talker is in one beam), judged on
	// the previous frame's SNRs.
	a.freezeAll = false
	for b := 0; b < n; b++ {
		if a.gate[b] < 0.5 {
			a.freezeAll = true
		}
	}
	for b := 0; b < n; b++ {
		a.cancel(b)
	}

	// Back to samples.
	for b := 0; b < n; b += 2 {
		b2 := b + 1
		for k := 0; k < araBins; k++ {
			ya := a.spec[b][k]
			var yb complex64
			if b2 < n {
				yb = a.spec[b2][k]
			}
			a.fft[k] = ya + complex(-imag(yb), real(yb)) // ya + j*yb
			if k > 0 && k < araFFT/2 {
				a.fft[araFFT-k] = complex(real(ya), -imag(ya)) + complex(imag(yb), real(yb)) // conj(ya) + j*conj(yb)
			}
		}
		araFFTc(a.fft, true)
		for i := 0; i < araFFT; i++ {
			a.acc[b][i] += real(a.fft[i]) * araWin[i]
			if b2 < n {
				a.acc[b2][i] += imag(a.fft[i]) * araWin[i]
			}
		}
	}
	for b := 0; b < n; b++ {
		a.fin[b] = append(a.fin[b], a.acc[b][:araHop]...)
		copy(a.acc[b][:araHop], a.acc[b][araHop:])
		clear(a.acc[b][araHop:])
	}
}

// cancel computes beam b's output spectrum: the beam's spectrum minus the
// prediction from its two references, and adapts the prediction when it is
// safe to.
func (a *ARA) cancel(b int) {
	x := &a.cur[b]
	y := &a.spec[b]
	*y = *x
	if !a.on {
		a.adapting[b] = false
		return
	}
	refs := araRefBeams(b, a.n)

	// Adaptation control, from the present frame's band powers.
	var px, pr float64
	for k := araBandLo; k <= araBandHi; k++ {
		px += cabs2(x[k])
		for _, r := range refs {
			pr += cabs2(a.cur[r][k])
		}
	}
	const sm = 0.9
	a.pxS[b] = sm*a.pxS[b] + (1-sm)*px
	a.prS[b] = sm*a.prS[b] + (1-sm)*pr
	interferenceDominates := a.prS[b] > ARARatio*a.pxS[b] && a.pxS[b] > 1
	// The references being louder than the target beam does not make the target
	// beam interference: a TV louder than the talker leaves them louder while
	// the talker speaks. What does is the filters' own residual. While they
	// are cancelling well it is small; a talker arriving is energy they
	// cannot predict, so the residual jumps and the filters are held.
	unexplained := a.resLast[b] > ARAUnexplained && a.resLast[b] > 2*a.resS[b]
	if unexplained {
		if a.frozen[b]++; a.frozen[b] > ARARelearn {
			// It has stayed: this is the room now, not a talker.
			a.resS[b], a.frozen[b], unexplained = a.resLast[b], 0, false
		}
	} else {
		a.frozen[b] = 0
	}
	if !interferenceDominates || unexplained {
		a.hold[b] = ARAHold
	} else if a.hold[b] > 0 {
		a.hold[b]--
	}
	ours := interferenceDominates && !unexplained && a.hold[b] == 0
	stock := !a.freezeAll
	var adapt bool
	switch a.gmode {
	case ARAGateStock:
		adapt = stock
	case ARAGateBoth:
		adapt = ours && stock
	default:
		adapt = ours
	}
	a.adapting[b] = adapt
	mu := float32(ARAMuSteady)
	if a.trained[b] < ARATrainFrames {
		mu = float32(ARAMu)
	}
	if a.gmode != ARAGateOurs {
		mu *= float32(a.gate[b])
	}
	if adapt {
		a.trained[b]++
	}
	leak := float32(1 - ARALeak)
	gmin := float32(ARAMinGain)
	gmin2 := gmin * gmin

	var pIn, pOut float64
	for k := 1; k <= araKMax; k++ {
		var pre, pim float32
		var norm float32 = 1 // a floor, so silence cannot divide by zero
		for j, rb := range refs {
			hv, wv := &a.h[k][rb], &a.w[b][j][k]
			for t := 0; t < araTaps; t++ {
				hr, hi := real(hv[t]), imag(hv[t])
				wr, wi := real(wv[t]), imag(wv[t])
				pre += wr*hr - wi*hi
				pim += wr*hi + wi*hr
				norm += hr*hr + hi*hi
			}
		}
		xr, xi := real(x[k]), imag(x[k])
		er, ei := xr-pre, xi-pim
		if adapt {
			fr, fi := er*mu/norm, ei*mu/norm
			for j, rb := range refs {
				hv, wv := &a.h[k][rb], &a.w[b][j][k]
				for t := 0; t < araTaps; t++ {
					hr, hi := real(hv[t]), imag(hv[t])
					// w = leak*w + conj(h) * f
					wv[t] = complex(real(wv[t])*leak+hr*fr+hi*fi, imag(wv[t])*leak+hr*fi-hi*fr)
				}
			}
		}
		// Never cancel a bin outright.
		px2, pe2 := xr*xr+xi*xi, er*er+ei*ei
		if pe2 < gmin2*px2 {
			g := gmin
			if pe2 > 0 {
				g = float32(math.Sqrt(float64(gmin2 * px2 / pe2)))
			} else {
				er, ei, pe2 = xr, xi, px2 // scaled below by gmin
			}
			er, ei = er*g, ei*g
			pe2 = er*er + ei*ei
		}
		y[k] = complex(er, ei)
		if k >= araBandLo && k <= araBandHi {
			pIn += float64(px2)
			pOut += float64(pe2)
		}
	}
	// Stock's SNR of the beam's own output, for next frame's gate.
	a.gframes[b]++
	if s := a.gsnr[b].addPower(pOut, a.gframes[b]); a.gframes[b] <= 1 {
		a.gsm[b] = s
	} else {
		a.gsm[b] = 0.75*a.gsm[b] + 0.25*s
	}
	a.gate[b] = araGate(a.gsm[b])
	if pIn > 1 && pOut > 0 {
		a.cut[b] = 0.95*a.cut[b] + 0.05*10*math.Log10(pIn/pOut)
		a.resLast[b] = pOut / pIn
		if adapt {
			a.resS[b] = 0.98*a.resS[b] + 0.02*a.resLast[b]
		}
	}
}

func cabs2(c complex64) float64 {
	return float64(real(c))*float64(real(c)) + float64(imag(c))*float64(imag(c))
}
