package main

// Every combination of the wake path's switches, on the same real capture, to
// see which gives the voice the best chance over what is around it.
//
// The capture is run through each path TWICE and only the second pass is
// measured: stock's merger and canceller learn their noise floors over the
// first several seconds, so a single 20s pass scores their start-up, not their
// running state. Speech is found once, on the microphones, and the same windows
// score every path.

import (
	"encoding/binary"
	"fmt"
	"math"
	"os"
	"path/filepath"

	"github.com/wilbowes/EchoMuse/internal/beamformer"
)

// dumpDir, when set, receives each path's scored (second-pass) output as raw
// 16kHz int16 mono, so other detectors can be run over exactly what the wake
// path produced (-dump).
var dumpDir string

func dumpRaw(name string, x []float64) {
	if dumpDir == "" {
		return
	}
	b := make([]byte, 2*len(x))
	for i, v := range x {
		binary.LittleEndian.PutUint16(b[2*i:], uint16(int16(math.Max(-32768, math.Min(32767, v)))))
	}
	if err := os.WriteFile(filepath.Join(dumpDir, name+".raw"), b, 0o644); err != nil {
		fmt.Println("dump:", err)
	}
}

func modeSweep(path string) {
	bufs, err := readRaw(path)
	if err != nil {
		fmt.Println(err)
		return
	}
	probe := beamformer.New()
	per := make([][]float64, beamformer.NumMics)
	for _, raw := range bufs {
		ch := probe.MicChannels(raw, 15.85, -1, nil)
		for m := range per {
			per[m] = append(per[m], s16Floats(ch[m])...)
		}
	}
	wins := speechWindows(per, rawHotDB)
	fmt.Printf("%s: %.1fs, speech at (s):", path, float64(len(bufs))*0.16)
	for _, w := range wins {
		fmt.Printf(" %.1f-%.1f", w[0], w[1])
	}
	fmt.Println()
	score := func(out []float64) (vb, tilt float64) {
		v, bg := windowBands(out, wins)
		var d []float64
		for i := range v {
			x := math.Max(math.Pow(10, v[i]/10)-math.Pow(10, bg[i]/10), math.Pow(10, v[i]/10)*0.02)
			d = append(d, 10*math.Log10(x+1e-9))
		}
		return v[3] - bg[3], d[4] - d[1]
	}
	var table *beamformer.SDWeights
	if stockTableFlag != "" {
		if table, err = beamformer.LoadStockTable(stockTableFlag); err != nil {
			fmt.Println("table:", err)
			return
		}
		fmt.Println("beams from stock's table")
	}
	dumpRaw("centre", per[6])
	vb, tilt := score(per[6])
	fmt.Printf("%-52s voice/bg %+5.1f  tilt %+5.1f\n", "centre mic (no beam)", vb, tilt)
	for _, rule := range []string{"mean", "stock"} {
		for _, ara := range []string{"off", "on kmax96", "on kmax40"} {
			for _, shape := range []bool{false, true} {
				beamformer.OutputShaping = shape
				if ara == "on kmax40" {
					beamformer.ARAKMax = 40
				} else {
					beamformer.ARAKMax = 96
				}
				b := beamformer.New()
				b.SetWakeBeam(true)
				b.SetWakeBeamAra(ara != "off")
				b.SetWakeBeamStock(rule == "stock")
				var out []float64
				for pass := 0; pass < 2; pass++ {
					out = out[:0]
					for _, raw := range bufs {
						mics := b.MicChannels(raw, 15.85, -1, nil)
						var o []byte
						for o = b.WakeBeam(mics); o == nil; o = b.WakeBeam(mics) {
						}
						if table != nil {
							b.UseWakeBeamWeights(table)
						}
						out = append(out, s16Floats(o)...)
					}
				}
				if rule == "stock" && ara == "on kmax40" && shape {
					dumpRaw("beam_stock_ara", out)
				}
				if rule == "stock" && ara == "off" && shape {
					dumpRaw("beam_stock", out)
				}
				vb, tilt := score(out)
				name := fmt.Sprintf("beam, %s rules, ARA %s, shaping %v", rule, ara, map[bool]string{true: "on", false: "off"}[shape])
				fmt.Printf("%-52s voice/bg %+5.1f  tilt %+5.1f\n", name, vb, tilt)
			}
		}
	}
	beamformer.OutputShaping, beamformer.ARAKMax = true, 40
}
