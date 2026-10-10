package spotify

import "encoding/json"

// The engine's newline-JSON control protocol (device/spotify/src/proto.rs).
// The shapes there are pinned by golden strings; the decoder here ignores
// fields it does not know, so the engine may add one without a firmware
// change. Nothing credential-like ever crosses it.

type event struct {
	Event string `json:"event"`

	State      string  `json:"state,omitempty"`
	PositionMs *uint32 `json:"position_ms,omitempty"`
	TrackURI   string  `json:"track_uri,omitempty"`

	URI        string   `json:"uri,omitempty"`
	Name       string   `json:"name,omitempty"`
	Artists    []string `json:"artists,omitempty"`
	Album      string   `json:"album,omitempty"`
	DurationMs uint32   `json:"duration_ms,omitempty"`
	Explicit   bool     `json:"explicit,omitempty"`
	CoverURL   string   `json:"cover_url,omitempty"`

	Level     *uint16 `json:"level,omitempty"`
	Connected bool    `json:"connected,omitempty"`
	User      string  `json:"user,omitempty"`

	Status string `json:"status,omitempty"`
	Detail string `json:"detail,omitempty"`

	On      bool   `json:"on,omitempty"`
	Context bool   `json:"context,omitempty"`
	Track   bool   `json:"track,omitempty"`
	Kind    string `json:"kind,omitempty"`
	Message string `json:"message,omitempty"`
}

type reply struct {
	Reply   string `json:"reply"`
	ID      uint64 `json:"id,omitempty"`
	Message string `json:"message,omitempty"`
}

// Track is what is playing, as the engine reports it.
type Track struct {
	URI        string   `json:"uri"`
	Name       string   `json:"name"`
	Artists    []string `json:"artists"`
	Album      string   `json:"album,omitempty"`
	DurationMs uint32   `json:"durationMs"`
	Explicit   bool     `json:"explicit,omitempty"`
	CoverURL   string   `json:"coverUrl,omitempty"`
}

// classify decodes one line from the engine; ok is false for anything that
// is neither an event nor a reply.
func classify(line []byte) (ev *event, rp *reply) {
	var probe struct {
		Event string `json:"event"`
		Reply string `json:"reply"`
	}
	if json.Unmarshal(line, &probe) != nil {
		return nil, nil
	}
	switch {
	case probe.Event != "":
		var e event
		if json.Unmarshal(line, &e) == nil {
			return &e, nil
		}
	case probe.Reply != "":
		var r reply
		if json.Unmarshal(line, &r) == nil {
			return nil, &r
		}
	}
	return nil, nil
}

// Spotify's volume is a u16 over the full range; the Echo's is a 0-100 value
// in its own law (outchain.StockVolumeValue on Radar, a percentage of
// volumeMax elsewhere). These convert between the two, and they ROUND rather
// than truncate.
//
// That is not a nicety. The level travels both ways — Spotify's slider down
// to the codec, and a physical button or Home Assistant back up to the phone
// — so the pair has to be a fixed point or a value that merely passes
// through comes back DIFFERENT, and the firmware then applies a change
// nobody made. Truncating both ways loses one step on 95 of the 101 values,
// always downward, which with librespot echoing a device-set level (see
// device/spotify/src/volume.rs) walked the volume down a step per lap and
// read as the volume moving on its own. Rounding round-trips all 101.
//
// Clamped because a wrong value here is inaudible at one end and silence at
// the other, and neither end should have to trust the other's arithmetic.

// PercentToU16 maps a 0-100 device value onto Spotify's u16 range.
func PercentToU16(pct int) uint16 {
	if pct <= 0 {
		return 0
	}
	if pct >= 100 {
		return 65535
	}
	return uint16((pct*65535 + 50) / 100)
}

// U16ToPercent maps Spotify's u16 volume onto a 0-100 device value.
func U16ToPercent(v uint16) int {
	return (int(v)*100 + 32767) / 65535
}
