// Package schedule owns durable schedules on one satellite. No server is
// involved in advancing time or deciding which alert is active.
package schedule

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

type envelope struct {
	Hash    string          `json:"sha256"`
	Payload json.RawMessage `json:"payload"`
}

func atomicFile(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	f, err := os.OpenFile(path+".tmp", os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	defer os.Remove(path + ".tmp")
	if _, err = f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err = f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	if err = os.Rename(path+".tmp", path); err != nil {
		return err
	}
	d, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}
func encode(s State) ([]byte, error) {
	b, e := json.Marshal(s)
	if e != nil {
		return nil, e
	}
	h := sha256.Sum256(b)
	return json.Marshal(envelope{hex.EncodeToString(h[:]), b})
}
func decode(b []byte, owner string) (State, error) {
	var env envelope
	var s State
	if len(b) > 512*1024 {
		return s, errors.New("schedule store too large")
	}
	if err := json.Unmarshal(b, &env); err != nil {
		return s, err
	}
	h := sha256.Sum256(env.Payload)
	if hex.EncodeToString(h[:]) != env.Hash {
		return s, errors.New("schedule checksum mismatch")
	}
	if err := json.Unmarshal(env.Payload, &s); err != nil {
		return s, err
	}
	defaults := DefaultSettings()
	if s.Settings.Sounds == nil {
		s.Settings.Sounds = defaults.Sounds
	}
	if s.Settings.TimerVolume == 0 {
		s.Settings.TimerVolume = defaults.TimerVolume
	}
	if s.Settings.AlarmVolume == 0 {
		s.Settings.AlarmVolume = defaults.AlarmVolume
	}
	if s.Version != 1 || s.Owner != owner {
		return s, errors.New("unsupported schedule schema or owner")
	}
	if len(s.Items) > MaxItems || len(s.Requests) > 128 {
		return s, errors.New("schedule bounds exceeded")
	}
	for _, i := range s.Items {
		if err := validate(i); err != nil {
			return s, fmt.Errorf("invalid stored item: %w", err)
		}
	}
	if err := validateSettings(s.Settings); err != nil {
		return s, err
	}
	return s, nil
}
func load(path, owner string) (State, error) {
	b, e := os.ReadFile(path)
	if e == nil {
		if s, err := decode(b, owner); err == nil {
			return s, nil
		}
	}
	b2, e2 := os.ReadFile(path + ".previous")
	if e2 == nil {
		return decode(b2, owner)
	}
	if errors.Is(e, os.ErrNotExist) && errors.Is(e2, os.ErrNotExist) {
		return State{Version: 1, Owner: owner, Items: []Item{}, Settings: DefaultSettings()}, nil
	}
	return State{}, errors.New("no valid schedule snapshot; preserve files and repair storage")
}
func save(path string, old, next State) error {
	previous, e := encode(old)
	if e != nil {
		return e
	}
	if e = atomicFile(path+".previous", previous); e != nil {
		return e
	}
	b, e := encode(next)
	if e != nil {
		return e
	}
	return atomicFile(path, b)
}
