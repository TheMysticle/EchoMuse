package beamformer

import (
	"bytes"
	"flag"
	"os"
	"testing"
)

var regenDesign = flag.Bool("regen-design", false, "rewrite fbf_design.bin from the current design")

func fbfPositions() [][2]float64 {
	pos := make([][2]float64, len(fbfChannels))
	for i, ch := range fbfChannels {
		pos[i] = MicPosition(ch)
	}
	return pos
}

// The embedded table must be what the solver gives now, or the firmware would
// ship a different beam set from the one every test and tool measures.
func TestEmbeddedDesignIsCurrent(t *testing.T) {
	pos := fbfPositions()
	solved := DesignSuperdirective(pos, fbfBeams, SDFloorOffsetDB)
	want := encodeDesign(pos, fbfBeams, solved)
	if *regenDesign {
		if err := os.WriteFile("fbf_design.bin", want, 0o644); err != nil {
			t.Fatal(err)
		}
		t.Skip("fbf_design.bin rewritten")
	}
	if !bytes.Equal(fbfDesignFile, want) {
		t.Fatal("fbf_design.bin is stale: go test ./internal/beamformer -run TestEmbeddedDesignIsCurrent -regen-design")
	}
	got, ok := embeddedDesign(pos, fbfBeams)
	if !ok {
		t.Fatal("embedded design rejected for the current parameters")
	}
	for b := range solved.W {
		for m := range solved.W[b] {
			if got.W[b][m] != solved.W[b][m] {
				t.Fatalf("beam %d mic %d differs after the round trip", b, m)
			}
		}
	}
}

// A changed parameter must fall back to solving, not load weights made for
// different ones.
func TestEmbeddedDesignIgnoredWhenParametersChange(t *testing.T) {
	old := SDRingWeight
	t.Cleanup(func() { SDRingWeight = old })
	SDRingWeight = old + 1
	if _, ok := embeddedDesign(fbfPositions(), fbfBeams); ok {
		t.Error("embedded design used with a different ring weight")
	}
}
