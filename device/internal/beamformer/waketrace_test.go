package beamformer

import (
	"strings"
	"sync"
	"testing"
	"time"
)

// run feeds one 160ms batch through the wake path the way the mic loop does
// (WakeBeam, then NoteWakePath) and stamps it at now.
func run(b *Beamformer, now time.Time) {
	b.WakeBeam(s16(zeros(NumMics, 2560)))
	b.NoteWakePath(now, b.OutputChannel(-1))
}

func TestWakeBeamInfoSaysWhatThePathDid(t *testing.T) {
	t0 := time.Unix(1000, 0)
	has := func(info WakeBeamInfo, want ...string) {
		t.Helper()
		s := info.String()
		for _, w := range want {
			if !strings.Contains(s, w) {
				t.Errorf("%q does not contain %q", s, w)
			}
		}
	}

	// Off: the single mic is named, and the beam is not.
	b := New()
	run(b, t0)
	has(b.WakeBeamAt(t0), "beam=off", "in=ch6")

	// On, design not finished: say so rather than claim the beam.
	b.fbfWant = true
	run(b, t0.Add(160*time.Millisecond))
	has(b.WakeBeamAt(t0.Add(160*time.Millisecond)), "beam=designing")

	// Ready, nothing standing out: the centre mic, and the weighting is out.
	// (fbfWant was set by hand above, so clear it for the helper to enable.)
	b.fbfWant = false
	enableWakeBeam(b)
	run(b, t0.Add(320*time.Millisecond))
	has(b.WakeBeamAt(t0.Add(320*time.Millisecond)), "beam=centre", "in=ch6", "sd=out")

	// A group engaged: its middle beam's direction, which mics, the weighting in, the design.
	b.fbf.cur, b.fbf.holdLeft = 2, 1<<30
	run(b, t0.Add(480*time.Millisecond))
	has(b.WakeBeamAt(t0.Add(480*time.Millisecond)),
		"beam=g2(205 deg)", "in=ch0,ch1,ch3,ch4", "sd=in[ring 3.0x +/-35 deg, floor +0.0dB]")

	// A turn holding the mic bypasses it, and the line must not claim otherwise.
	b.lockedChannel = 3
	run(b, t0.Add(640*time.Millisecond))
	has(b.WakeBeamAt(t0.Add(640*time.Millisecond)), "bypassed", "in=ch3")
}

// The canceller's state rides the same fragment: off, on but not in the path,
// and on in the path with how far it has cut and whether it is learning.
func TestWakeBeamInfoSaysWhatTheCancellerIsDoing(t *testing.T) {
	info := WakeBeamInfo{Known: true, Setting: true, Ready: true, From: 2, To: 2, Az: 90, Chans: []int{0, 1, 3, 4}}
	if s := info.String(); !strings.Contains(s, "ara=off") {
		t.Error(s)
	}
	info.Ara, info.AraCut, info.AraAdapting = true, 7.25, true
	if s := info.String(); !strings.Contains(s, "ara=on[cut 7.2dB learning]") && !strings.Contains(s, "ara=on[cut 7.3dB learning]") {
		t.Error(s)
	}
	info.AraAdapting = false
	if s := info.String(); !strings.Contains(s, "frozen") {
		t.Error(s)
	}
	info.From, info.To = -1, -1
	if s := info.String(); !strings.Contains(s, "ara=on(not in path)") {
		t.Error(s)
	}
}

// A turn heard through the beam says so, and names the beam held for it, rather
// than claiming the beam was bypassed.
func TestWakeBeamInfoSaysATurnUsedTheBeam(t *testing.T) {
	info := WakeBeamInfo{Known: true, Setting: true, Ready: true, Locked: true, Turn: true, From: 6, To: 6, Az: 55, Chans: []int{0, 1, 3, 4}}
	s := info.String()
	if !strings.Contains(s, "turn:g6(55 deg)") || strings.Contains(s, "bypassed") {
		t.Error(s)
	}
	info.Turn = false
	if s := info.String(); !strings.Contains(s, "bypassed") {
		t.Error(s)
	}
}

func TestWakeBeamInfoReportsCalibrationAsApplied(t *testing.T) {
	old := micCalDir
	t.Cleanup(func() { micCalDir = old })
	micCalDir = t.TempDir() // nothing to read
	b := New()
	enableWakeBeam(b)
	b.fbf.cur, b.fbf.holdLeft = 1, 1<<30
	run(b, time.Unix(1, 0))
	if s := b.WakeBeamAt(time.Unix(1, 0)).String(); !strings.Contains(s, "cal=off") {
		t.Errorf("uncalibrated: %s", s)
	}

	micCalDir = writeMicCal(t, radarMicCal[:])
	b = New()
	enableWakeBeam(b)
	b.fbf.cur, b.fbf.holdLeft = 1, 1<<30
	run(b, time.Unix(1, 0))
	if s := b.WakeBeamAt(time.Unix(1, 0)).String(); !strings.Contains(s, "cal=on") {
		t.Errorf("calibrated: %s", s)
	}
}

// A beam that changed within the batch says so: the crossing may be on either
// side of the change.
func TestWakeBeamInfoShowsAChangeWithinABatch(t *testing.T) {
	info := WakeBeamInfo{Known: true, Setting: true, Ready: true, From: -1, To: 3, Az: 135, Chans: []int{0, 1, 3, 4}}
	if s := info.String(); !strings.Contains(s, "beam=centre->g3(135 deg)") {
		t.Error(s)
	}
	info.From, info.To = 3, -1
	if s := info.String(); !strings.Contains(s, "beam=g3->centre") || !strings.Contains(s, "sd=out") {
		t.Error(s)
	}
}

// The lookup is by the crossing's own time: the batch it was in, not the latest
// one, and nothing from before the record starts.
func TestWakeBeamAtFindsTheBatchTheCrossingWasIn(t *testing.T) {
	b := New()
	enableWakeBeam(b)
	t0 := time.Unix(2000, 0)
	for k := 0; k < 6; k++ {
		b.fbf.cur, b.fbf.holdLeft = k, 1<<30
		run(b, t0.Add(time.Duration(k)*160*time.Millisecond))
	}
	// 30ms into batch 3's period, as a frame stamped just after it would be.
	got := b.WakeBeamAt(t0.Add(3*160*time.Millisecond + 30*time.Millisecond))
	if got.To != 3 {
		t.Errorf("crossing in batch 3 reported beam %d", got.To)
	}
	if got := b.WakeBeamAt(t0.Add(-time.Second)); got.Known {
		t.Error("a time before the record must be unknown, not the oldest entry")
	}
	// Older than the ring holds: gone, not wrapped onto something newer.
	for k := 6; k < 6+traceLen+4; k++ {
		run(b, t0.Add(time.Duration(k)*160*time.Millisecond))
	}
	if got := b.WakeBeamAt(t0.Add(160 * time.Millisecond)); got.Known {
		t.Errorf("an entry %d batches old survived a ring of %d", traceLen+9, traceLen)
	}
}

// The mic goroutine writes and the scorer's callback reads; -race checks it.
func TestWakeTraceIsSafeAcrossGoroutines(t *testing.T) {
	b := New()
	enableWakeBeam(b)
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < 500; i++ {
			b.WakeBeamAt(time.Now())
		}
	}()
	for i := 0; i < 500; i++ {
		b.NoteWakePath(time.Now(), 6)
	}
	wg.Wait()
}
