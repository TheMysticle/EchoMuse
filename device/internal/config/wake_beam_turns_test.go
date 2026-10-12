package config

import "testing"

func TestWakeBeamTurnsDefaultsOffAndIsIndependent(t *testing.T) {
	d := &Device{}
	d.Apply(ConfigMessage{})
	if got := d.Snapshot().WakeBeamTurns; got == nil || *got {
		t.Fatalf("wakeBeamTurns must default off, snapshot says %v", got)
	}
	on := true
	d.Apply(ConfigMessage{WakeBeamTurns: &on})
	snap := d.Snapshot()
	if snap.WakeBeamTurns == nil || !*snap.WakeBeamTurns {
		t.Fatal("true wakeBeamTurns was not applied")
	}
	// Independent of the other two: turning turns on turns neither on.
	if (snap.WakeBeam != nil && *snap.WakeBeam) || (snap.WakeBeamAra != nil && *snap.WakeBeamAra) {
		t.Error("wakeBeamTurns turned the beam or the canceller on")
	}
	d.Apply(ConfigMessage{})
	if got := d.Snapshot().WakeBeamTurns; got == nil || !*got {
		t.Fatal("a sparse push changed wakeBeamTurns")
	}
	off := false
	d.Apply(ConfigMessage{WakeBeamTurns: &off})
	if got := d.Snapshot().WakeBeamTurns; got == nil || *got {
		t.Fatalf("false wakeBeamTurns was not applied: %v", got)
	}
}
