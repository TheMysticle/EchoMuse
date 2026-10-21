package config

import "testing"

func TestVoicePrebufferConfigPreservesAbsentAndRejectsInvalid(t *testing.T) {
	d := &Device{VoicePrebufferMs: 1024}
	for _, ms := range []int{256, 512, 1024} {
		d.Apply(ConfigMessage{VoicePrebufferMs: &ms})
		d.Apply(ConfigMessage{})
		if d.VoicePrebufferMs != ms {
			t.Fatalf("lost %d setting", ms)
		}
	}
	for _, ms := range []int{-1, 0, 127, 1025} {
		d.Apply(ConfigMessage{VoicePrebufferMs: &ms})
		if d.VoicePrebufferMs != 1024 {
			t.Fatalf("accepted invalid %d", ms)
		}
	}
	snapshot := d.Snapshot()
	ms := 256
	d.Apply(ConfigMessage{VoicePrebufferMs: &ms})
	if snapshot.VoicePrebufferMs == nil || *snapshot.VoicePrebufferMs != 1024 {
		t.Fatal("snapshot changed with live configuration")
	}
}
