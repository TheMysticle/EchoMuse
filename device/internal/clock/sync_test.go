package clock

import "testing"

func TestTimeTrustCanOnlySurviveTheSameKernelBoot(t *testing.T) {
	if !sameBoot("boot-A\n", "boot-A\n") {
		t.Fatal("process restart lost valid boot identity")
	}
	if sameBoot("boot-A", "boot-B") {
		t.Fatal("power cycle reused clock trust")
	}
	if sameBoot("", "") {
		t.Fatal("missing boot IDs trusted")
	}
}

func TestChangedCoalescesAndNeverBlocks(t *testing.T) {
	for len(Changed()) > 0 {
		<-Changed()
	}
	notify()
	notify() // nobody reading: must neither block nor queue two
	n := 0
	for len(Changed()) > 0 {
		<-Changed()
		n++
	}
	if n != 1 {
		t.Fatalf("%d signals kept, want 1", n)
	}
}
