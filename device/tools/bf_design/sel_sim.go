package main

// What the wake path does to a quiet, distant talker in a living room.
//
// The other reports use a steady interferer and an oracle that picks the best
// beam. A home has neither: the noise is ambient plus transients from anywhere (a
// keyboard, a door), and the beam is chosen by the firmware's own SNR group
// selection, which those transients can move. This drives the real wake path
// (beamformer.WakeBeam: selection, crossfades, the canceller) through such a
// scene and measures what reaches the wake word model: the talker's level, the
// noise's level and their ratio, against the centre mic, which is what the
// single-mic path hears.
//
// The canceller and the selection make the path nonlinear, so the talker's
// contribution is the output with the talker minus the output without, over the
// same noise.

import (
	"flag"
	"fmt"
	"math"
	"math/rand"
	"sort"

	"github.com/wilbowes/EchoMuse/internal/beamformer"
)

var (
	selAz       float64
	selTalker   float64
	selDiffuse  float64
	selSelf     float64
	selTrans    float64
	selTV       float64
	selTVAz     float64
	selDRR      float64
	selBursts   int
	selSeedList string
)

func selFlags() {
	flag.Float64Var(&selAz, "sel.az", -1, "talker azimuth in degrees (-1 = a different one each burst)")
	flag.Float64Var(&selTalker, "sel.talker", 120, "talker rms at a mic, int16 units")
	flag.Float64Var(&selDiffuse, "sel.diffuse", 100, "diffuse room noise rms at a mic")
	flag.Float64Var(&selSelf, "sel.self", 30, "mic self-noise rms")
	flag.Float64Var(&selTrans, "sel.trans", 600, "transient (keyboard, door) rms while it lasts")
	flag.Float64Var(&selTV, "sel.tv", 0, "a steady music-like source (a TV) at this rms at a mic; 0 = none")
	flag.Float64Var(&selTVAz, "sel.tvaz", 200, "the TV's azimuth in degrees")
	flag.Float64Var(&selDRR, "sel.drr", 99, "talker direct-to-reverberant ratio in dB (99 = a plane wave); below that a diffuse tail from every direction is added, as for a voice from another room")
	flag.IntVar(&selBursts, "sel.bursts", 8, "talker bursts")
}

// selRun is one pass of the scene through a path, returning per-batch output.
type selRun struct {
	out    [][]float64 // per mic path: the output stream
	engage []int       // per batch, the beam in use (-1 centre)
}

func runWakePath(sig [][]float64, ara bool, pins []int) ([]float64, []int) {
	b := beamformer.New()
	b.SetWakeBeam(true)
	b.SetWakeBeamAra(ara)
	// -grp.mode / -ara.gate choose stock's rules for the whole run.
	b.SetWakeBeamStock(beamformer.GroupMode == beamformer.GroupStock || beamformer.ARAGate == beamformer.ARAGateStock)
	const batch = 128 // one selection block, so a replay is exact
	n := len(sig[0])
	s16 := func(x []float64) []byte {
		o := make([]byte, len(x)*2)
		for i, v := range x {
			q := int16(math.Max(-32768, math.Min(32767, math.Round(v))))
			o[i*2], o[i*2+1] = byte(uint16(q)), byte(uint16(q)>>8)
		}
		return o
	}
	var out []float64
	var idx []int
	for s := 0; s+batch <= n; s += batch {
		mics := make([][]byte, beamformer.NumMics)
		for m := range mics {
			mics[m] = s16(sig[m][s : s+batch])
		}
		// The design is made in the background: wait for it before the scene
		// starts, so the first seconds are not the design's.
		var o []byte
		for {
			if pins != nil && len(idx) < len(pins) {
				b.PinWakeGroup(pins[len(idx)])
			}
			if o = b.WakeBeam(mics); o != nil {
				break
			}
		}
		for i := 0; i < batch; i++ {
			out = append(out, float64(int16(uint16(o[i*2])|uint16(o[i*2+1])<<8)))
		}
		idx = append(idx, b.WakeBeamIndex())
	}
	return out, idx
}

func selReport(rng *rand.Rand) {
	const (
		n        = 1 << 20
		lo, hi   = 100.0, 4000.0
		burstLen = 19200 // 1.2s
	)
	pos := micPositions("all")
	fmt.Printf("building a %.0fs scene...\n", float64(n)/sampleRate)
	noise := sumSignals(
		scale(Diffuse(rng, pos, n, 24, lo, hi), selDiffuse),
		scale(SelfNoise(rng, len(pos), n, lo, hi), selSelf))

	if selTV > 0 {
		tv := NewSource(rng, 1<<16, lo, hi, speechShape)
		m := scale(tv.AtMics(pos, selTVAz, 0), selTV)
		for ch := range noise {
			for i := 0; i < n; i++ {
				noise[ch][i] += m[ch][i%len(m[ch])]
			}
		}
	}

	// Transients: 50ms broadband clacks from anywhere, spaced 1.5-4.5s.
	var trans []struct{ at int }
	for t := 3 * 16000; t < n-2*16000; t += 16000*3/2 + rng.Intn(16000*3) {
		trans = append(trans, struct{ at int }{t})
	}
	for _, tr := range trans {
		src := NewSource(rng, 4096, lo, hi, flat)
		mics := scale(src.AtMics(pos, rng.Float64()*360, 0), selTrans)
		for m := range noise {
			for i := 0; i < 800 && tr.at+i < n; i++ {
				w := math.Sin(math.Pi * float64(i) / 800)
				noise[m][tr.at+i] += mics[m][i] * w * w
			}
		}
	}

	// Talker: 1.2s bursts of speech-shaped noise with a syllabic envelope.
	type burst struct {
		start int
		az    float64
	}
	var bursts []burst
	speech := make([][]float64, len(pos))
	for m := range speech {
		speech[m] = make([]float64, n)
	}
	for k := 0; k < selBursts; k++ {
		start := 8*16000 + k*7*16000/1 + rng.Intn(16000)
		if start+burstLen >= n {
			break
		}
		az := selAz
		if az < 0 {
			az = rng.Float64() * 360
		}
		bursts = append(bursts, burst{start, az})
		src := NewSource(rng, 1<<15, lo, hi, speechShape)
		mics := scale(src.AtMics(pos, az, 0), selTalker)
		for m := range speech {
			for i := 0; i < burstLen; i++ {
				t := float64(i) / sampleRate
				env := 0.15 + 0.85*math.Abs(math.Sin(2*math.Pi*3.5*t))
				fade := math.Min(1, math.Min(float64(i), float64(burstLen-i))/800)
				speech[m][start+i] += mics[m][i] * env * fade / 0.65
			}
		}
	}
	both := sumSignals(noise, speech)

	design := beamformer.DesignSuperdirective(pos[:0], 8, 0) // only for the look directions
	_ = design

	type path struct {
		name string
		run  func(sig [][]float64, pins []int) ([]float64, []int)
	}
	paths := []path{
		{"centre mic (the single-mic path)", func(sig [][]float64, _ []int) ([]float64, []int) { return sig[6], nil }},
		{"wake beam", func(sig [][]float64, pins []int) ([]float64, []int) { return runWakePath(sig, false, pins) }},
		{"wake beam + ARA", func(sig [][]float64, pins []int) ([]float64, []int) { return runWakePath(sig, true, pins) }},
	}
	type res struct{ talker, noise, snr []float64 }
	results := make([]res, len(paths))
	var engaged [][]int
	for pi, p := range paths {
		// The noise-only run is held on the groups the talker run chose, so the
		// two differ by the talker alone.
		outB, idx := p.run(both, nil)
		outN, _ := p.run(noise, idx)
		engaged = append(engaged, idx)
		for _, bu := range bursts {
			lo2, hi2 := bu.start+4000, bu.start+burstLen // after the first quarter second
			pw := func(x []float64) float64 {
				var s float64
				for i := lo2; i < hi2 && i < len(x); i++ {
					s += x[i] * x[i]
				}
				return s / float64(hi2-lo2)
			}
			pn, pb := pw(outN), pw(outB)
			tal := math.Max(pb-pn, 1e-9)
			results[pi].talker = append(results[pi].talker, db(tal))
			results[pi].noise = append(results[pi].noise, db(pn))
			results[pi].snr = append(results[pi].snr, db(tal/pn))
		}
	}

	fmt.Printf("\nscene: talker %.0f rms at a mic, diffuse noise %.0f, self-noise %.0f, %d transients of %.0f rms; %d talker bursts of 1.2s\n",
		selTalker, selDiffuse, selSelf, len(trans), selTrans, len(bursts))
	fmt.Printf("%-34s %14s %14s %12s %12s\n", "", "talker level", "noise level", "SNR", "worst SNR")
	mean := func(v []float64) float64 {
		var s float64
		for _, x := range v {
			s += x
		}
		return s / float64(len(v))
	}
	for pi, p := range paths {
		r := results[pi]
		worst := append([]float64(nil), r.snr...)
		sort.Float64s(worst)
		fmt.Printf("%-34s %+11.1f dB %+11.1f dB %+9.1f dB %+9.1f dB\n", p.name,
			mean(r.talker)-mean(results[0].talker), mean(r.noise)-mean(results[0].noise),
			mean(r.snr)-mean(results[0].snr), worst[0]-mean(results[0].snr))
	}
	fmt.Printf("(levels relative to the centre mic; SNR is the talker's power over the noise's in the same window)\n")

	// Which output the wake path was on while the talker spoke.
	az := make([]float64, 8)
	copy(az, beamformer.SDAzimuths)
	for pi := 1; pi < len(paths); pi++ {
		var neutral, near, far, total int
		for _, bu := range bursts {
			for k := bu.start / 2560; k <= (bu.start+burstLen)/2560 && k*20 < len(engaged[pi]); k++ {
				total++
				i := engaged[pi][k*20]
				switch {
				case i < 0:
					neutral++
				default:
					covered := false
					for j := 0; j < 3; j++ { // group i is beams i, i+1, i+2
						if angDiff(az[(i+j)%8], bu.az) <= 30 {
							covered = true
						}
					}
					if covered {
						near++
					} else {
						far++
					}
				}
			}
		}
		fmt.Printf("%-34s while the talker spoke: centre mic %2.0f%%, a group with a beam within 30° of the talker %2.0f%%, a group not covering him %2.0f%%\n",
			paths[pi].name, 100*float64(neutral)/float64(total), 100*float64(near)/float64(total), 100*float64(far)/float64(total))
	}
}
