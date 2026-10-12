package config

import "testing"

func TestWakeBeamAraDefaultsOffAndAppliesBothValues(t *testing.T) {
	d := &Device{}
	d.Apply(ConfigMessage{})
	if got := d.Snapshot().WakeBeamAra; got == nil || *got {
		t.Fatalf("wakeBeamAra must default off, snapshot says %v", got)
	}
	on := true
	d.Apply(ConfigMessage{WakeBeamAra: &on})
	if got := d.Snapshot().WakeBeamAra; got == nil || !*got {
		t.Fatal("true wakeBeamAra was not applied")
	}
	d.Apply(ConfigMessage{})
	if got := d.Snapshot().WakeBeamAra; got == nil || !*got {
		t.Fatal("a sparse push changed wakeBeamAra")
	}
	// The two settings are independent: the canceller can be asked for with the
	// beam off, and says nothing about it.
	if got := d.Snapshot().WakeBeam; got == nil || *got {
		t.Error("turning the canceller on must not turn the beam on")
	}
	off := false
	d.Apply(ConfigMessage{WakeBeamAra: &off})
	if got := d.Snapshot().WakeBeamAra; got == nil || *got {
		t.Fatalf("false wakeBeamAra was not applied: %v", got)
	}
}
