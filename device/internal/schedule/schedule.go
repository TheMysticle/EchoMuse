package schedule

import (
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"regexp"
	"sync"
	"time"
	_ "time/tzdata"
)

// The bundled Home Assistant Voice PE alert is the timer default and the
// alarm fallback when the stock firmware earcon is unavailable.
//
//go:embed timer_finished.pcm
var TimerAlarmSound []byte

const MaxItems = 64
const MaxSeconds = int64(3650 * 24 * 3600)

var idPattern = regexp.MustCompile(`^[a-zA-Z0-9_-]{1,80}$`)
var assetPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)

type Rule struct {
	Interval int64 `json:"interval,omitempty"`
	Days     []int `json:"days,omitempty"`
	Hour     int   `json:"hour"`
	Minute   int   `json:"minute"`
}
type Item struct {
	ID            string `json:"id"`
	Kind          string `json:"kind"`
	Label         string `json:"label"`
	Message       string `json:"message,omitempty"`
	State         string `json:"state"`
	Due           int64  `json:"due"`
	Duration      int64  `json:"duration,omitempty"`
	Remaining     int64  `json:"remaining,omitempty"`
	Zone          string `json:"timezone"`
	Rule          *Rule  `json:"rule,omitempty"`
	Sound         string `json:"sound,omitempty"`
	SpeechVersion int    `json:"speech_version,omitempty"`
	Speech        string `json:"speech,omitempty"`
	RingLeft      int64  `json:"ring_remaining,omitempty"`
	RingStart     int64  `json:"ring_started,omitempty"`
	Fired         int64  `json:"fired,omitempty"`
	Terminal      int64  `json:"terminal,omitempty"`
	Occurrence    int64  `json:"occurrence,omitempty"`
	Enabled       bool   `json:"enabled"`
}
type Settings struct {
	ReminderRepeat  bool                `json:"reminder_repeat"`
	ReminderRepeats int                 `json:"reminder_repeats"`
	Colors          map[string][3]uint8 `json:"colors"`
	Sounds          map[string]string   `json:"sounds"`
	Snooze          int64               `json:"snooze"`
	Ramp            int64               `json:"ramp"`
	RingCap         int64               `json:"ring_cap"`
	Retention       int64               `json:"retention"`
	Grace           int64               `json:"grace"`
	Volume          float64             `json:"volume"`
	TimerVolume     float64             `json:"timer_volume"`
	AlarmVolume     float64             `json:"alarm_volume"`
}

func DefaultSettings() Settings {
	return Settings{Sounds: map[string]string{}, Colors: map[string][3]uint8{"timer": {0, 188, 212}, "alarm": {255, 152, 0}, "reminder": {156, 39, 176}}, Snooze: 540, Ramp: 20, RingCap: 600, Retention: 3600, Grace: 900, Volume: 0.5, TimerVolume: 0.25, AlarmVolume: 0.2}
}
func validateSettings(s Settings) error {
	for kind, id := range s.Sounds {
		if (kind != "timer" && kind != "alarm" && kind != "reminder") || (id != "" && !assetPattern.MatchString(id)) {
			return errors.New("invalid global alert sound")
		}
	}
	if s.ReminderRepeats < 0 || s.ReminderRepeats > 10 {
		return errors.New("reminder repeats must be between 0 and 10")
	}
	if s.Snooze < 1 || s.Snooze > 86400 || s.Ramp < 0 || s.Ramp > 300 || s.RingCap < 1 || s.RingCap > 3600 || s.Retention < 0 || s.Retention > 604800 || s.Grace < 0 || s.Grace > 86400 || s.Volume < 0.01 || s.Volume > 1 || s.TimerVolume < 0.01 || s.TimerVolume > 1 || s.AlarmVolume < 0.01 || s.AlarmVolume > 1 {
		return errors.New("invalid alert settings")
	}
	for _, k := range []string{"timer", "alarm", "reminder"} {
		if _, ok := s.Colors[k]; !ok {
			return errors.New("missing alert color")
		}
	}
	return nil
}

type State struct {
	Version  int      `json:"version"`
	Owner    string   `json:"owner"`
	Revision uint64   `json:"revision"`
	Items    []Item   `json:"items"`
	Settings Settings `json:"settings"`
	Requests []string `json:"requests"`
}
type Snapshot struct {
	State
	TimeValid bool   `json:"time_valid"`
	Active    string `json:"active"`
	Error     string `json:"error,omitempty"`
}
type Command struct {
	Kind     string    `json:"kind,omitempty"`
	Request  string    `json:"request"`
	Owner    string    `json:"owner"`
	Action   string    `json:"action"`
	ID       string    `json:"id,omitempty"`
	Item     *Item     `json:"item,omitempty"`
	Seconds  int64     `json:"seconds,omitempty"`
	Revision *uint64   `json:"revision,omitempty"`
	Settings *Settings `json:"settings,omitempty"`
}
type Engine struct {
	mu             sync.Mutex
	state          State
	path           string
	now            func() time.Time
	valid          func() bool
	deadlines      map[string]time.Time
	rings          map[string]time.Time
	restoredQueued map[string]bool
	persist        func(State, State) error
	fault          string
	wake           chan struct{}
}

func Open(path, owner string, now func() time.Time, valid func() bool) (*Engine, error) {
	s, e := load(path, owner)
	if e != nil {
		return nil, e
	}
	x := &Engine{state: s, path: path, now: now, valid: valid, deadlines: map[string]time.Time{}, rings: map[string]time.Time{}, restoredQueued: map[string]bool{}, wake: make(chan struct{}, 1)}
	for _, i := range s.Items {
		if i.State == "queued" {
			x.restoredQueued[i.ID] = true
		}
	}
	x.persist = func(a, b State) error { return save(path, a, b) }
	return x, nil
}
func clone(s State) State { b, _ := json.Marshal(s); var c State; _ = json.Unmarshal(b, &c); return c }
func validate(i Item) error {
	if !idPattern.MatchString(i.ID) || (i.Kind != "timer" && i.Kind != "alarm" && i.Kind != "reminder") || len(i.Label) > 160 || len(i.Message) > 2048 {
		return errors.New("invalid item identity, type, or text")
	}
	if i.Duration < 0 || i.Duration > MaxSeconds || i.Remaining < 0 || i.Remaining > MaxSeconds || i.Due < 0 || i.Due > 4133980800 {
		return errors.New("invalid duration or date")
	}
	if _, e := time.LoadLocation(i.Zone); e != nil {
		return errors.New("invalid timezone")
	}
	for _, a := range []string{i.Sound, i.Speech} {
		if a != "" && !assetPattern.MatchString(a) {
			return errors.New("invalid asset id")
		}
	}
	if r := i.Rule; r != nil {
		if r.Interval < 0 || r.Interval > MaxSeconds || r.Hour < 0 || r.Hour > 23 || r.Minute < 0 || r.Minute > 59 || len(r.Days) > 7 {
			return errors.New("invalid recurrence")
		}
		if r.Interval == 0 && len(r.Days) == 0 {
			return errors.New("recurrence needs days or interval")
		}
		for _, d := range r.Days {
			if d < 0 || d > 6 {
				return errors.New("invalid weekday")
			}
		}
	}
	return nil
}
func priority(k string) int {
	switch k {
	case "alarm":
		return 0
	case "timer":
		return 1
	default:
		return 2
	}
}
func active(s State) string {
	var best *Item
	for n := range s.Items {
		i := &s.Items[n]
		if i.State != "ringing" && i.State != "queued" {
			continue
		}
		if best == nil || priority(i.Kind) < priority(best.Kind) || (priority(i.Kind) == priority(best.Kind) && (i.Occurrence < best.Occurrence || (i.Occurrence == best.Occurrence && i.ID < best.ID))) {
			best = i
		}
	}
	if best != nil {
		return best.ID
	}
	return ""
}
func (e *Engine) Snapshot() Snapshot { e.mu.Lock(); defer e.mu.Unlock(); return e.snapshot() }
func (e *Engine) snapshot() Snapshot {
	return Snapshot{clone(e.state), e.valid(), active(e.state), e.fault}
}
func (e *Engine) commit(s State) error {
	s.Revision = e.state.Revision + 1
	if err := e.persist(e.state, s); err != nil {
		e.fault = "schedule persistence failed"
		return err
	}
	e.state = s
	e.fault = ""
	return nil
}
func (e *Engine) Apply(c Command) (Snapshot, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if c.Owner != e.state.Owner {
		return e.snapshot(), errors.New("wrong satellite")
	}
	if c.Action == "list" {
		return e.snapshot(), nil
	}
	if !idPattern.MatchString(c.Request) {
		return e.snapshot(), errors.New("request id required")
	}
	for _, id := range e.state.Requests {
		if id == c.Request {
			return e.snapshot(), nil
		}
	}
	if c.Revision != nil && *c.Revision != e.state.Revision {
		return e.snapshot(), errors.New("stale revision")
	}
	s := clone(e.state)
	now := e.now()
	idx := -1
	for n := range s.Items {
		if s.Items[n].ID == c.ID {
			idx = n
			break
		}
	}
	var deadline time.Time
	clearDeadline := false
	switch c.Action {
	case "settings":
		if c.Settings == nil {
			return e.snapshot(), errors.New("settings required")
		}
		if err := validateSettings(*c.Settings); err != nil {
			return e.snapshot(), err
		}
		s.Settings = *c.Settings
		// Older clients omit sounds; changing another setting must not clear them.
		if s.Settings.Sounds == nil {
			s.Settings.Sounds = e.state.Settings.Sounds
		}
	case "create", "edit":
		if c.Item == nil {
			return e.snapshot(), errors.New("item required")
		}
		i := *c.Item
		if err := validate(i); err != nil {
			return e.snapshot(), err
		}
		if c.Action == "create" {
			for _, old := range s.Items {
				if old.ID == i.ID {
					return e.snapshot(), errors.New("item id already exists")
				}
			}
			if len(s.Items) >= MaxItems {
				return e.snapshot(), errors.New("64 item limit reached")
			}
		} else if idx < 0 || i.ID != c.ID {
			return e.snapshot(), errors.New("item not found")
		}
		if c.Action == "edit" {
			old := s.Items[idx]
			if i.Due == old.Due && i.Duration == old.Duration && i.Zone == old.Zone && reflect.DeepEqual(i.Rule, old.Rule) {
				i.State = old.State
				i.Fired = old.Fired
				i.Terminal = old.Terminal
				i.Occurrence = old.Occurrence
				i.Enabled = old.Enabled
				i.Remaining = old.Remaining
				i.RingLeft = old.RingLeft
				i.RingStart = old.RingStart
				s.Items[idx] = i
				break
			}
		}

		i.Enabled = true
		i.State = "scheduled"
		i.Fired = 0
		i.Terminal = 0
		if i.Duration > 0 {
			deadline = now.Add(time.Duration(i.Duration) * time.Second)
			if e.valid() {
				i.Due = deadline.Unix()
			} else {
				i.Due = 0
			}
		} else if i.Due <= now.Unix() && i.Rule == nil {
			return e.snapshot(), errors.New("deadline must be in the future")
		}
		if i.Duration == 0 && !e.valid() {
			return e.snapshot(), errors.New("waiting for valid time")
		}
		if i.Rule != nil && i.Due == 0 {
			i.Due = nextOccurrence(i, now).Unix()
		}
		if c.Action == "create" {
			s.Items = append(s.Items, i)
		} else {
			s.Items[idx] = i
			clearDeadline = true
		}
		c.ID = i.ID
	case "cancel_all":
		for n := range s.Items {
			if s.Items[n].Kind == c.Kind || (c.Kind == "" && s.Items[n].Kind == "timer") {
				s.Items[n].State = "canceled"
				s.Items[n].Terminal = now.Unix()
			}
		}
	default:
		if idx < 0 {
			return e.snapshot(), errors.New("item not found")
		}
		i := &s.Items[idx]
		switch c.Action {
		case "cancel":
			i.State = "canceled"
			i.Terminal = now.Unix()
			clearDeadline = true
		case "complete":
			if i.Rule != nil && i.Enabled {
				after := now
				if i.Due > now.Unix() {
					after = time.Unix(i.Due, 0)
				}
				i.Occurrence = i.Due
				i.Due = nextOccurrence(*i, after).Unix()
				i.State = "scheduled"
				i.Fired = 0
				i.Terminal = now.Unix()
			} else {
				i.State = "completed"
				i.Fired = now.Unix()
				i.Terminal = now.Unix()
			}
			clearDeadline = true
		case "dismiss":
			if i.State != "ringing" && i.State != "snoozed" {
				return e.snapshot(), errors.New("item is not ringing")
			}
			finish(i, now)
		case "snooze":
			if i.State != "ringing" {
				return e.snapshot(), errors.New("item is not ringing")
			}
			secs := c.Seconds
			if secs == 0 {
				secs = s.Settings.Snooze
			}
			if secs < 1 || secs > 86400 {
				return e.snapshot(), errors.New("invalid snooze duration")
			}
			i.State = "snoozed"
			i.Due = now.Unix() + secs
			deadline = now.Add(time.Duration(secs) * time.Second)
		case "pause":
			if i.Kind != "timer" || i.State != "scheduled" {
				return e.snapshot(), errors.New("only running timers can pause")
			}
			i.Remaining = i.Due - now.Unix()
			if d, ok := e.deadlines[i.ID]; ok {
				i.Remaining = int64(d.Sub(now).Seconds() + 0.999)
			}
			if i.Remaining < 1 {
				i.Remaining = 1
			}
			i.State = "paused"
			clearDeadline = true
		case "resume":
			if i.State != "paused" {
				return e.snapshot(), errors.New("timer is not paused")
			}
			i.State = "scheduled"
			deadline = now.Add(time.Duration(i.Remaining) * time.Second)
			i.Due = deadline.Unix()
			i.Remaining = 0
		case "adjust":
			if i.Kind != "timer" || (i.State != "scheduled" && i.State != "paused") || c.Seconds == 0 || c.Seconds > MaxSeconds || c.Seconds < -MaxSeconds {
				return e.snapshot(), errors.New("invalid timer adjustment")
			}
			if i.State == "paused" {
				i.Remaining += c.Seconds
				if i.Remaining < 1 || i.Remaining > MaxSeconds {
					return e.snapshot(), errors.New("invalid remaining time")
				}
			} else {
				left := i.Due - now.Unix()
				if d, ok := e.deadlines[i.ID]; ok {
					left = int64(d.Sub(now).Seconds() + 0.999)
				}
				left += c.Seconds
				if left < 1 || left > MaxSeconds {
					return e.snapshot(), errors.New("invalid remaining time")
				}
				deadline = now.Add(time.Duration(left) * time.Second)
				i.Due = deadline.Unix()
			}
		case "disable":
			i.Enabled = false
			i.State = "disabled"
			clearDeadline = true
		case "enable":
			if !e.valid() {
				return e.snapshot(), errors.New("waiting for valid time")
			}
			if i.Rule == nil {
				return e.snapshot(), errors.New("only recurring schedules can be enabled")
			}
			i.Enabled = true
			i.State = "scheduled"
			i.Due = nextOccurrence(*i, now).Unix()
		default:
			return e.snapshot(), fmt.Errorf("unknown action %q", c.Action)
		}
	}
	s.Requests = append(s.Requests, c.Request)
	if len(s.Requests) > 128 {
		s.Requests = s.Requests[len(s.Requests)-128:]
	}
	e.arbitrate(&s, now)
	if err := e.commit(s); err != nil {
		return e.snapshot(), err
	}
	if clearDeadline {
		delete(e.deadlines, c.ID)
	}
	if !deadline.IsZero() {
		e.deadlines[c.ID] = deadline
	}
	e.Wake()
	return e.snapshot(), nil
}

// Changed is signalled whenever a command changed the store, or Wake is
// called, so a loop sleeping until the next due time can re-plan.
func (e *Engine) Changed() <-chan struct{} { return e.wake }

// Wake asks the loop to re-plan. It never blocks.
func (e *Engine) Wake() {
	select {
	case e.wake <- struct{}{}:
	default:
	}
}

// Revision is the store revision, for callers that only need to know whether
// anything changed.
func (e *Engine) Revision() uint64 { e.mu.Lock(); defer e.mu.Unlock(); return e.state.Revision }

const (
	ringPoll = 100 * time.Millisecond // a ringing alert is watched closely
	wallNear = time.Second            // re-plan this often when a wall-clock alert is imminent
	wallFar  = 10 * time.Second       // ... and this often when it is not
	wallSoon = 2 * time.Minute
	maxIdle  = time.Minute
)

// Sleep is how long until Advance next has something to do, never more than
// maxIdle. Timers run on the monotonic clock and are exact. An alarm or
// reminder waits for a wall-clock second, and a timer sleeping to it does not
// notice the wall clock being stepped, so those re-plan on a clock.Changed
// signal and, for a step nothing announced, every second when due within two
// minutes and every ten otherwise. While anything is ringing it is 100ms,
// as the loop that watched every tick did.
func (e *Engine) Sleep() time.Duration {
	e.mu.Lock()
	defer e.mu.Unlock()
	now, valid := e.now(), e.valid()
	next := maxIdle
	soonest := func(d time.Duration) {
		if d < next {
			next = d
		}
	}
	for _, i := range e.state.Items {
		switch i.State {
		case "ringing", "queued":
			soonest(ringPoll)
			continue
		case "completed", "missed", "canceled":
			if valid {
				base := i.Fired
				if base == 0 {
					base = i.Terminal
				}
				if i.State == "canceled" {
					soonest(0)
				} else {
					soonest(time.Unix(base+e.state.Settings.Retention, 0).Sub(now))
				}
			}
			continue
		case "scheduled", "waiting_for_time", "snoozed":
		default:
			continue
		}
		if !i.Enabled {
			continue
		}
		d, mono := e.deadlines[i.ID]
		switch {
		case mono:
			soonest(d.Sub(now))
		case !valid:
			if i.State != "waiting_for_time" {
				soonest(0)
			}
			soonest(wallFar) // the clock becoming trusted is signalled; this is the net
		case i.Due == 0:
		case i.Kind == "timer":
			soonest(0) // Advance gives it a monotonic deadline
		default:
			until := time.Unix(i.Due, 0).Sub(now)
			soonest(until)
			if until <= wallSoon {
				soonest(wallNear)
			} else {
				soonest(wallFar)
			}
		}
	}
	return max(next, 0)
}
func finish(i *Item, now time.Time) {
	i.Terminal = now.Unix()
	if i.Rule != nil && i.Enabled {
		i.Due = nextOccurrence(*i, now).Unix()
		i.State = "scheduled"
		i.Fired = 0
	} else {
		i.State = "completed"
	}
}

// Calendar rules scan real instants on a local date. This selects the first
// repeated time and advances missing DST times to the first valid local minute.
func nextOccurrence(i Item, after time.Time) time.Time {
	r := i.Rule
	if r.Interval > 0 {
		base := i.Occurrence
		if base == 0 {
			base = i.Due
		}
		if base <= 0 {
			base = after.Unix()
		}
		n := (after.Unix()-base)/r.Interval + 1
		if n < 1 {
			n = 1
		}
		return time.Unix(base+n*r.Interval, 0)
	}
	loc, _ := time.LoadLocation(i.Zone)
	local := after.In(loc)
	for day := 0; day < 370; day++ {
		date := time.Date(local.Year(), local.Month(), local.Day()+day, 12, 0, 0, 0, loc)
		allowed := false
		for _, d := range r.Days {
			if d == int(date.Weekday()) {
				allowed = true
			}
		}
		if !allowed {
			continue
		}
		midnight := date.Add(-14 * time.Hour) // Start before this local day even when midnight is skipped.
		for m := 0; m < 40*60; m++ {
			t := midnight.Add(time.Duration(m) * time.Minute)
			l := t.In(loc)
			if l.Day() != date.Day() || l.Month() != date.Month() {
				if t.Before(date) {
					continue
				}
				break
			}
			if l.Hour()*60+l.Minute() >= r.Hour*60+r.Minute {
				if t.After(after) {
					return t
				}
				break
			}
		}
	}
	return after.Add(24 * time.Hour)
}

// Advance moves time forward: it fires what is due, ends what has rung long
// enough and drops what has been kept long enough. It reports whether the
// stored state changed. It does not copy the state out; callers that need it
// ask for a Snapshot, which most wakeups do not.
func (e *Engine) Advance() bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.advance()
}

// Tick is Advance followed by a Snapshot.
func (e *Engine) Tick() (Snapshot, bool) {
	e.mu.Lock()
	defer e.mu.Unlock()
	changed := e.advance()
	return e.snapshot(), changed
}
func (e *Engine) advance() bool {
	s := clone(e.state)
	previousDeadlines := make(map[string]time.Time, len(e.deadlines))
	for k, v := range e.deadlines {
		previousDeadlines[k] = v
	}
	previousRings := make(map[string]time.Time, len(e.rings))
	for k, v := range e.rings {
		previousRings[k] = v
	}
	now := e.now()
	valid := e.valid()
	changed := false
	for n := range s.Items {
		i := &s.Items[n]
		if i.State == "queued" && valid && e.restoredQueued[i.ID] {
			if now.Unix()-i.Fired > s.Settings.Grace {
				i.State = "missed"
				i.Terminal = now.Unix()
				if i.Rule != nil {
					finish(i, now)
				}
				changed = true
			}
			delete(e.restoredQueued, i.ID)
		}
		if i.State == "ringing" {
			capTime, live := e.rings[i.ID]
			if !live && valid {
				capTime = now.Add(time.Duration(i.RingStart+i.RingLeft-now.Unix()) * time.Second)
				e.rings[i.ID] = capTime
				live = true
			}

			if live && valid {
				start := now.Unix() + int64(capTime.Sub(now).Seconds()) - i.RingLeft
				if start-i.RingStart > 2 || i.RingStart-start > 2 {
					i.RingStart = start
					if i.Fired < 1704067200 {
						i.Fired = start
					}
					changed = true
				}
			}
			if live && !now.Before(capTime) {
				finish(i, now)
				delete(e.rings, i.ID)
				changed = true
			}
		}
		if i.State != "scheduled" && i.State != "waiting_for_time" && i.State != "snoozed" {
			continue
		}
		if !i.Enabled {
			continue
		}
		d, mono := e.deadlines[i.ID]
		if !valid && !mono {
			if i.State != "waiting_for_time" {
				i.State = "waiting_for_time"
				changed = true
			}
			continue
		}
		if valid && mono && (i.Due == 0 || now.Add(d.Sub(now)).Unix()-i.Due > 2 || i.Due-now.Add(d.Sub(now)).Unix() > 2) {
			i.Due = now.Add(d.Sub(now)).Unix()
			changed = true
		}
		if !mono && i.Due == 0 {
			continue
		} // A timer interrupted before its UTC anchor is unknowable.
		if i.State == "waiting_for_time" {
			i.State = "scheduled"
			changed = true
		}
		if i.Kind == "timer" && !mono && valid {
			d = now.Add(time.Duration(i.Due-now.Unix()) * time.Second)
			e.deadlines[i.ID] = d
			mono = true
		}
		due := valid && now.Unix() >= i.Due
		if mono {
			due = !now.Before(d)
		}
		if due {
			late := now.Unix() - i.Due
			if mono {
				late = int64(now.Sub(d).Seconds())
			}
			if late > s.Settings.Grace {
				i.State = "missed"
				i.Terminal = now.Unix()
				if i.Rule != nil {
					finish(i, now)
				}
			} else {
				wasSnoozed := i.State == "snoozed"
				i.State = "queued"
				i.RingLeft = s.Settings.RingCap
				i.RingStart = 0
				e.rings[i.ID] = now.Add(time.Duration(s.Settings.RingCap) * time.Second)
				i.Fired = now.Unix()
				if !wasSnoozed {
					i.Occurrence = i.Due
				}
				i.Terminal = 0
			}
			delete(e.deadlines, i.ID)
			changed = true
		}
	}
	if e.arbitrate(&s, now) {
		changed = true
	}
	for _, i := range s.Items {
		if i.State == "ringing" {
			if _, ok := e.rings[i.ID]; !ok && valid {
				e.rings[i.ID] = now.Add(time.Duration(i.RingLeft) * time.Second)
			}
		} else {
			delete(e.rings, i.ID)
		}
	}
	kept := s.Items[:0]
	for _, i := range s.Items {
		terminal := i.State == "completed" || i.State == "missed" || i.State == "canceled"
		base := i.Fired
		if base == 0 {
			base = i.Terminal
		}
		if terminal && valid && (i.State == "canceled" || now.Unix()-base >= s.Settings.Retention) {
			delete(e.deadlines, i.ID)
			changed = true
			continue
		}
		kept = append(kept, i)
	}
	s.Items = kept
	if changed {
		if err := e.commit(s); err != nil {
			e.deadlines = previousDeadlines
			e.rings = previousRings
			return false
		}
	}
	return changed
}
func (e *Engine) Active() (Item, Settings, bool) {
	e.mu.Lock()
	defer e.mu.Unlock()
	id := active(e.state)
	for _, i := range e.state.Items {
		if i.ID == id && i.State == "ringing" {
			if !e.valid() {
				if _, live := e.rings[id]; !live {
					return Item{}, e.state.Settings, false
				}
			}
			return i, e.state.Settings, true
		}
	}
	return Item{}, e.state.Settings, false
}

// arbitrate preserves a queued alert's full ringing budget. Higher-priority
// arrivals can preempt without consuming the lower-priority item's cap.
func (e *Engine) arbitrate(s *State, now time.Time) bool {
	id := active(*s)
	changed := false
	for n := range s.Items {
		i := &s.Items[n]
		if i.State == "ringing" && i.ID != id {
			if d, ok := e.rings[i.ID]; ok {
				i.RingLeft = int64(d.Sub(now).Seconds() + 0.999)
			} else {
				i.RingLeft = i.RingStart + i.RingLeft - now.Unix()
			}
			if i.RingLeft < 1 {
				i.RingLeft = 1
			}
			i.State = "queued"
			i.RingStart = 0
			changed = true
		}
		if i.State == "queued" && i.ID == id {
			i.State = "ringing"
			i.RingStart = now.Unix()
			changed = true
		}
	}
	return changed
}
