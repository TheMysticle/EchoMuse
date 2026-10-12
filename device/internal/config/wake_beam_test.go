package config

import "testing"

func TestWakeBeamDefaultsOffAndAppliesBothValues(t *testing.T) {
	d := &Device{}
	d.Apply(ConfigMessage{})
	if got := d.Snapshot().WakeBeam; got == nil || *got {
		t.Fatalf("wake beam must default off, snapshot says %v", got)
	}

	on := true
	d.Apply(ConfigMessage{WakeBeam: &on})
	if got := d.Snapshot().WakeBeam; got == nil || !*got {
		t.Fatal("true wakeBeam was not applied")
	}

	// A push that does not mention it leaves it alone.
	d.Apply(ConfigMessage{})
	if got := d.Snapshot().WakeBeam; got == nil || !*got {
		t.Fatal("a sparse push changed wakeBeam")
	}

	off := false
	d.Apply(ConfigMessage{WakeBeam: &off})
	if got := d.Snapshot().WakeBeam; got == nil || *got {
		t.Fatalf("false wakeBeam was not applied: %v", got)
	}
}
