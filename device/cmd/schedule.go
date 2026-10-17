package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"github.com/wilbowes/EchoMuse/internal/bindings/speaker"
	"github.com/wilbowes/EchoMuse/internal/client"
	"github.com/wilbowes/EchoMuse/internal/clock"
	"github.com/wilbowes/EchoMuse/internal/schedule"
	"github.com/wilbowes/EchoMuse/internal/server"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const scheduleDir = "/data/local/etc/echomuse/schedules"

func startSchedules(ctx context.Context, owner string, c *client.ControlClient, s *server.Server, p *speaker.PcmSpeaker) *schedule.Engine {
	e, err := schedule.Open(filepath.Join(scheduleDir, "state.json"), owner, time.Now, clock.Trusted)
	if err != nil {
		log.Printf("[schedule] cannot open durable store: %v", err)
		c.OnScheduler(func(raw json.RawMessage) map[string]any {
			var m struct {
				ID string `json:"id"`
			}
			_ = json.Unmarshal(raw, &m)
			return map[string]any{"id": m.ID, "error": "schedule store needs repair"}
		})
		return nil
	}
	assets := filepath.Join(scheduleDir, "assets")
	c.OnScheduler(func(raw json.RawMessage) map[string]any {
		var m struct {
			ID      string           `json:"id"`
			Command schedule.Command `json:"command"`
			Asset   string           `json:"asset"`
			Data    string           `json:"data"`
		}
		var identity struct {
			ID string `json:"id"`
		}
		_ = json.Unmarshal(raw, &identity)
		reply := map[string]any{"id": identity.ID}
		if len(raw) > 6*1024*1024 || json.Unmarshal(raw, &m) != nil {
			reply["error"] = "invalid schedule request"
			return reply
		}
		reply["id"] = m.ID
		if m.Asset != "" {
			b, err := base64.StdEncoding.DecodeString(m.Data)
			if err == nil {
				err = schedule.PutAsset(assets, m.Asset, b)
			}
			if err != nil {
				reply["error"] = err.Error()
			}
			return reply
		}
		if m.Command.Action == "prune_assets" {
			if m.Command.Owner != owner {
				reply["error"] = "wrong satellite"
				return reply
			}
			snapshot := e.Snapshot()
			_, err := schedule.PruneAssets(assets, snapshot.Items, snapshot.Settings)
			if err != nil {
				reply["error"] = err.Error()
			}
			reply["snapshot"] = snapshot
			return reply
		}

		if m.Command.Settings != nil {
			if err := schedule.CheckSounds(assets, *m.Command.Settings); err != nil {
				reply["error"] = err.Error()
				return reply
			}
		}
		if m.Command.Item != nil {
			for _, id := range []string{m.Command.Item.Sound, m.Command.Item.Speech} {
				if id != "" {
					if _, err := schedule.ReadAsset(assets, id); err != nil {
						reply["error"] = "sound/speech asset is not cached on device"
						return reply
					}
				}
			}
		}
		snap, err := e.Apply(m.Command)
		reply["snapshot"] = snap
		if err != nil {
			reply["error"] = err.Error()
		}
		return reply
	})
	servers := []string{"time.cloudflare.com", "pool.ntp.org"}
	if v := os.Getenv("ECHOMUSE_NTP_SERVERS"); v != "" {
		servers = strings.Split(v, ",")
	}
	go clock.SyncNetwork(ctx, servers)
	go func() {
		// Sleep until the engine's next due time instead of polling: the loop
		// wakes for a due item, for a command or clock step that changes what
		// is due, and for the 2s state report while connected. Ringing is
		// still watched every 100ms (Engine.Sleep).
		t := time.NewTimer(0)
		defer t.Stop()
		last := ""
		var lastRevision uint64
		report := time.Time{}
		for {
			e.Advance()
			item, settings, ok := e.Active()
			key := ""
			if ok {
				key = fmt.Sprintf("%s/%d/%d/%s/%s/%v", item.ID, item.Occurrence, item.Fired, item.Sound, item.Speech, settings)
			}
			if key != last {
				last = key
				if ok {
					sound := schedule.Sound(assets, item.Kind, settings)
					speech, _ := schedule.ReadAsset(assets, item.Speech)
					volume := settings.Volume
					if item.Kind == "timer" {
						volume = settings.TimerVolume
					} else if item.Kind == "alarm" {
						volume = settings.AlarmVolume
					}
					if item.Kind == "reminder" {
						repeats := 0
						if settings.ReminderRepeat {
							repeats = settings.ReminderRepeats
						}
						p.SetLocalReminder(sound, speech, volume, repeats)
					} else {
						p.SetLocalAlert(sound, speech, volume, settings.Ramp)
					}
					color := settings.Colors[item.Kind]
					s.SetAlertRing(&color)
				} else {
					p.StopLocalAlert()
					s.SetAlertRing(nil)
				}
			}
			if key != "" && ok && item.Kind == "reminder" && !p.LocalAlertActive() {
				// Finish this occurrence durably and let queued alerts proceed.
				_, err := e.Apply(schedule.Command{Owner: owner, Request: fmt.Sprintf("played_%d", time.Now().UnixNano()), Action: "dismiss", ID: item.ID})
				if err != nil {
					log.Printf("[schedule] reminder completion failed: %v", err)
				}
			}
			wait := e.Sleep()
			if c.IsConnected() {
				if revision := e.Revision(); revision != lastRevision || time.Since(report) > 2*time.Second {
					if c.SendSchedule(e.Snapshot()) == nil {
						lastRevision = revision
						report = time.Now()
					}
				}
				wait = min(wait, max(0, 2*time.Second-time.Since(report)))
			}
			// A floor, so a state Advance cannot settle can never spin.
			t.Reset(max(wait, 5*time.Millisecond))
			select {
			case <-ctx.Done():
				p.StopLocalAlert()
				s.SetAlertRing(nil)
				return
			case <-e.Changed():
			case <-clock.Changed():
			case <-t.C:
			}
			if !t.Stop() {
				select {
				case <-t.C:
				default:
				}
			}
		}
	}()
	return e
}
