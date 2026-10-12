package beamformer

// The wake beams are a pure function of the array geometry and a handful of
// design constants, and the solve takes seconds on the Echo's A53. Stock avoids
// that wait by loading a precomputed table; this does the same with OUR design,
// generated at development time and embedded (33KB). Nothing in the file comes
// from Amazon's firmware.
//
// The file records the parameters it was made with. If any differs at run time
// (the bf_design tool changes them, a future edit forgets to regenerate) the
// design is solved as before, and TestEmbeddedDesignIsCurrent fails the build
// of a stale file. Regenerate with:
//
//	go test ./internal/beamformer -run TestEmbeddedDesignIsCurrent -regen-design

import (
	"bytes"
	_ "embed"
	"encoding/binary"
	"fmt"
	"math"
)

//go:embed fbf_design.bin
var fbfDesignFile []byte

// designFingerprint names everything the design depends on.
func designFingerprint(pos [][2]float64, nBeams int) string {
	az := make([]float64, nBeams)
	for b := range az {
		az[b] = sdAzimuth(b, nBeams)
	}
	return fmt.Sprintf("EMFBF1 beams=%d mics=%d bins=%d ring=%v sector=%v floor=%v az=%v pos=%v",
		nBeams, len(pos), araBins, SDRingWeight, SDSectorDeg, SDFloorOffsetDB, az, pos)
}

// encodeDesign is the file's format: the fingerprint and a newline, then every
// weight as little-endian float32 pairs in W[beam][mic][bin] order.
func encodeDesign(pos [][2]float64, nBeams int, d *SDWeights) []byte {
	var buf bytes.Buffer
	buf.WriteString(designFingerprint(pos, nBeams) + "\n")
	for b := range d.W {
		for m := range d.W[b] {
			for _, w := range d.W[b][m] {
				binary.Write(&buf, binary.LittleEndian, [2]float32{real(w), imag(w)})
			}
		}
	}
	return buf.Bytes()
}

// embeddedDesign returns the embedded design when it was made with the current
// parameters, and false when it must be solved instead.
func embeddedDesign(pos [][2]float64, nBeams int) (*SDWeights, bool) {
	want := designFingerprint(pos, nBeams) + "\n"
	if !bytes.HasPrefix(fbfDesignFile, []byte(want)) {
		return nil, false
	}
	body := fbfDesignFile[len(want):]
	if len(body) != nBeams*len(pos)*araBins*8 {
		return nil, false
	}
	out := &SDWeights{Mics: len(pos), pos: pos, Azimuths: make([]float64, nBeams), W: make([][][araBins]complex64, nBeams)}
	for b := range out.W {
		out.W[b] = make([][araBins]complex64, len(pos))
		out.Azimuths[b] = sdAzimuth(b, nBeams)
		for m := range out.W[b] {
			for k := 0; k < araBins; k++ {
				i := ((b*len(pos)+m)*araBins + k) * 8
				re := math.Float32frombits(binary.LittleEndian.Uint32(body[i:]))
				im := math.Float32frombits(binary.LittleEndian.Uint32(body[i+4:]))
				out.W[b][m][k] = complex(re, im)
			}
		}
	}
	return out, true
}
