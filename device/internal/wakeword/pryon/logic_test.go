package pryon

import (
	"testing"
	"time"
)

func TestParseScoreFromTheDecodersLogLine(t *testing.T) {
	cases := []struct {
		in   string
		want float32
		ok   bool
	}{
		{"DecoderID=dec: recognition_id=dec-0: {KWS} segmentation_score=-inf,classifier_score=0.998347,active_threshold=0.923,detection_type=Accept", 0.998347, true},
		{"classifier_score=0.759516,active_threshold=0.69,detection_type=NearMiss", 0.759516, true},
		{"classifier_score=1,detection_type=Accept", 1, true},
		{"classifier_score=", 0, false},
		{"classifier_score=nan,x", 0, false},
		{"classifier_score=inf", 0, false},
		{"no score here", 0, false},
		{"", 0, false},
	}
	for _, c := range cases {
		got, ok := ParseScore(c.in)
		if ok != c.ok || (ok && got != c.want) {
			t.Errorf("ParseScore(%q) = %v, %v; want %v, %v", c.in, got, ok, c.want, c.ok)
		}
	}
}

func TestAnAcceptIsAWakeAtTheBarInForce(t *testing.T) {
	if c, bar := Judge(TypeAccept, 0.95, false); !c || bar != AcceptBar {
		t.Errorf("idle accept: %v %v", c, bar)
	}
	if c, bar := Judge(TypeAccept, 0.95, true); !c || bar != PlaybackBar {
		t.Errorf("accept while playing reports the playback bar: %v %v", c, bar)
	}
}

// Stock lowers the bar to 0.864 while audio plays, through a client property
// this engine does not set; near misses stand in for it.
func TestNearMissesCountOnlyWhilePlayingAndOnlyOverThePlaybackBar(t *testing.T) {
	cases := []struct {
		score   float32
		playing bool
		want    bool
	}{
		{0.80, false, false}, // idle: a near miss is a near miss
		{0.89, false, false},
		{0.89, true, true},
		{0.864, true, true},
		{0.863, true, false},
		{0.70, true, false},
	}
	for _, c := range cases {
		if got, _ := Judge(TypeNearMiss, c.score, c.playing); got != c.want {
			t.Errorf("near miss %.3f playing=%v: %v, want %v", c.score, c.playing, got, c.want)
		}
	}
}

func TestAnUnknownResultTypeIsNeverAWake(t *testing.T) {
	for _, typ := range []uint32{1, 3, 7, 0xffffffff} {
		if c, _ := Judge(typ, 0.99, true); c {
			t.Errorf("type %d treated as a wake", typ)
		}
	}
}

func TestModelNameSelectsTheEngineAndOnlyIt(t *testing.T) {
	if !IsModel("pryon_alexa") || !IsModel("  pryon_alexa ") {
		t.Error("the engine's own name must select it")
	}
	for _, other := range []string{"alexa_v0.1", "hey_jarvis_v0.1", "/data/oww/pryon_alexa.onnx", "pryon", ""} {
		if IsModel(other) {
			t.Errorf("%q selects the Pryon engine", other)
		}
	}
}

func TestTimeRingMapsASampleToItsCaptureTime(t *testing.T) {
	r := newTimeRing()
	base := time.Unix(1000, 0)
	for i := 0; i < 5; i++ {
		r.add(uint64((i+1)*1280), base.Add(time.Duration(i)*80*time.Millisecond))
	}
	fb := time.Unix(9, 0)
	if got := r.when(1, fb); !got.Equal(base) {
		t.Errorf("first chunk: %v", got)
	}
	if got := r.when(1280, fb); !got.Equal(base) {
		t.Errorf("a chunk's own last sample belongs to it: %v", got)
	}
	if got := r.when(1281, fb); !got.Equal(base.Add(80 * time.Millisecond)) {
		t.Errorf("one past the boundary is the next chunk: %v", got)
	}
	if got := r.when(99999, fb); !got.Equal(fb) {
		t.Errorf("a sample not pushed yet falls back: %v", got)
	}
}

func TestTimeRingForgetsTheOldestWhenFull(t *testing.T) {
	r := newTimeRing()
	base := time.Unix(1000, 0)
	for i := 0; i < timeRingLen+10; i++ {
		r.add(uint64((i+1)*1280), base.Add(time.Duration(i)*time.Millisecond))
	}
	fb := time.Unix(9, 0)
	if got := r.when(1, fb); !got.Equal(base.Add(10 * time.Millisecond)) {
		// sample 1 is older than everything kept: the oldest kept chunk answers
		t.Errorf("an evicted sample resolves to the oldest kept chunk, got %v", got)
	}
	if got := r.when(uint64((timeRingLen+10)*1280), fb); !got.Equal(base.Add(time.Duration(timeRingLen+9) * time.Millisecond)) {
		t.Errorf("newest chunk wrong: %v", got)
	}
}
