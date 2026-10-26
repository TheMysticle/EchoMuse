package wifi

import (
	"testing"
	"time"
)

func TestRejoinIfDeaf(t *testing.T) {
	now := time.Unix(1_800_000_000, 0)
	runs := 0
	emos, assoc := true, true
	rejoinNow = func() time.Time { return now }
	rejoinOnEmOS = func() bool { return emos }
	rejoinAssociated = func() bool { return assoc }
	rejoinRun = func() (string, error) { runs++; return "OK\n", nil }
	t.Cleanup(func() {
		rejoinNow, rejoinOnEmOS, rejoinAssociated = time.Now, onEmOS, associated
		rejoinRun = func() (string, error) { return wpaCli("reassociate") }
		rejoin.last = time.Time{}
	})

	if RejoinIfDeaf(rejoinAfterMisses-1) || runs != 0 {
		t.Fatal("rejoined before enough misses")
	}
	if !RejoinIfDeaf(rejoinAfterMisses) || runs != 1 {
		t.Fatal("did not rejoin at the threshold")
	}
	now = now.Add(rejoinEvery - time.Second)
	if RejoinIfDeaf(rejoinAfterMisses+5) || runs != 1 {
		t.Fatal("rejoined again inside the rate limit")
	}
	now = now.Add(2 * time.Second)
	if !RejoinIfDeaf(rejoinAfterMisses+6) || runs != 2 {
		t.Fatal("did not rejoin after the rate limit")
	}

	now = now.Add(rejoinEvery)
	assoc = false
	if RejoinIfDeaf(rejoinAfterMisses) || runs != 2 {
		t.Fatal("rejoined while not associated")
	}
	assoc, emos = true, false
	if RejoinIfDeaf(rejoinAfterMisses) || runs != 2 {
		t.Fatal("drove the supplicant on FireOS")
	}
}
