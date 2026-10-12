package main

// Reads a raw capture of the microphone array (the firmware's
// /data/local/tmp/capture.raw, or tools/capture_mics: interleaved S24_3LE, 9
// channels, 16kHz) and says what each microphone and each version of the wake
// path makes of it, by band against the centre mic.
//
// It exists because the recordings a command is saved as cannot say where a loss
// is: in one capsule, in the geometry the beams assume, or in the selection. A
// voice that loses its upper range through the beam (measured 2026-10-13 in
// recordings with a TV on, not in a quiet room) needs the microphones themselves.

import (
	"fmt"
	"math"
	"os"
	"sort"

	"github.com/wilbowes/EchoMuse/internal/beamformer"
)

const rawFrameBytes = 9 * 3

// readRaw splits an interleaved S24_3LE 9-channel capture into whole 2560-frame
// buffers, the size the firmware's mic loop hands over (160ms).
func readRaw(path string) ([][]byte, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	const buf = 2560 * rawFrameBytes
	var out [][]byte
	for i := 0; i+buf <= len(b); i += buf {
		out = append(out, b[i:i+buf])
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("%s: under one 160ms buffer (%d bytes)", path, len(b))
	}
	return out, nil
}

var rawBandEdges = []float64{60, 200, 400, 800, 1600, 3200, 6400}

// bandLevels is the mean power of x in each rawBandEdges band, dB, from a Hann
// periodogram average.
func bandLevels(x []float64) []float64 {
	const n = 1024
	acc := make([]float64, n/2+1)
	var segs int
	for s := 0; s+n <= len(x); s += n / 2 {
		buf := make([]complex128, n)
		for i := 0; i < n; i++ {
			w := 0.5 - 0.5*math.Cos(2*math.Pi*float64(i)/float64(n-1))
			buf[i] = complex(x[s+i]*w, 0)
		}
		fft(buf, false)
		for k := range acc {
			acc[k] += real(buf[k])*real(buf[k]) + imag(buf[k])*imag(buf[k])
		}
		segs++
	}
	out := make([]float64, len(rawBandEdges)-1)
	for b := range out {
		var p float64
		for k := range acc {
			f := float64(k) * sampleRate / n
			if f >= rawBandEdges[b] && f < rawBandEdges[b+1] {
				p += acc[k]
			}
		}
		out[b] = 10 * math.Log10(p/float64(max(segs, 1))+1e-9)
	}
	return out
}

func s16Floats(b []byte) []float64 {
	o := make([]float64, len(b)/2)
	for i := range o {
		o[i] = float64(int16(uint16(b[i*2]) | uint16(b[i*2+1])<<8))
	}
	return o
}

// rawReport prints each microphone's band levels against the centre mic, then
// the wake path's, in the modes worth comparing.
func rawReport(path string, from, to float64) {
	bufs, err := readRaw(path)
	if err != nil {
		fmt.Println(err)
		return
	}
	lo, hi := int(from/0.16), len(bufs)
	if to > 0 {
		hi = min(hi, int(to/0.16))
	}
	if lo >= hi {
		fmt.Println("empty window")
		return
	}
	bufs = bufs[lo:hi]
	fmt.Printf("%s: %.1fs from %.1fs\n", path, float64(len(bufs))*0.16, from)

	header := func() {
		fmt.Printf("%-34s", "")
		for b := 0; b < len(rawBandEdges)-1; b++ {
			fmt.Printf(" %5.0f-%-5.0f", rawBandEdges[b], rawBandEdges[b+1])
		}
		fmt.Println()
	}

	// Every microphone, through the firmware's own extraction (gain applied).
	bf := beamformer.New()
	per := make([][]float64, beamformer.NumMics)
	for _, raw := range bufs {
		ch := bf.MicChannels(raw, 15.85, -1, nil)
		for m := range per {
			per[m] = append(per[m], s16Floats(ch[m])...)
		}
	}
	ref := bandLevels(per[6])
	fmt.Println("\neach microphone against the centre mic (ch6), dB by band (a capsule or port that is dull shows here):")
	header()
	for m := range per {
		l := bandLevels(per[m])
		fmt.Printf("ch%d%-30s", m, map[bool]string{true: " (centre)", false: ""}[m == 6])
		for b := range l {
			fmt.Printf(" %+11.1f", l[b]-ref[b])
		}
		fmt.Println()
	}

	// The wake path in the modes worth comparing.
	type mode struct {
		name       string
		ara, stock bool
	}
	modes := []mode{{"beam, ARA off", false, false}, {"beam, ARA on", true, false}, {"beam + Radar's rules, ARA off", false, true}, {"beam + Radar's rules + ARA", true, true}}
	fmt.Println("\nthe wake path against the centre mic (the whole window), dB by band, and the groups it used:")
	header()
	for _, md := range modes {
		b := beamformer.New()
		b.SetWakeBeam(true)
		b.SetWakeBeamAra(md.ara)
		b.SetWakeBeamStock(md.stock)
		var out []float64
		used := map[int]int{}
		for _, raw := range bufs {
			mics := b.MicChannels(raw, 15.85, -1, nil)
			var o []byte
			for o = b.WakeBeam(mics); o == nil; o = b.WakeBeam(mics) {
			}
			out = append(out, s16Floats(o)...)
			used[b.WakeBeamIndex()]++
		}
		l := bandLevels(out)
		fmt.Printf("%-34s", md.name)
		for i := range l {
			fmt.Printf(" %+11.1f", l[i]-ref[i])
		}
		var ks []int
		for k := range used {
			ks = append(ks, k)
		}
		sort.Ints(ks)
		fmt.Printf("   groups:")
		for _, k := range ks {
			fmt.Printf(" %d:%d%%", k, 100*used[k]/len(bufs))
		}
		fmt.Println()
	}
}
