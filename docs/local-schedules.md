# Local timers, reminders, and alarms

EchoMuse Scheduler stores schedules and audio on the Echo, under
`/data/local/etc/echomuse/schedules/`. Already saved alerts run while the
controller and Home Assistant are offline. The action button dismisses a
ringing item with a tap; hold for at least 800 ms to snooze it. Microphone mute
continues to control the microphones independently.

This feature needs the updated **device firmware and controller**, plus the
**EchoMuse Scheduler companion integration** for Home Assistant. The companion
creates a reminder `todo` entity and live timer/alarm `sensor` entities on a scheduler device linked to the ESPHome satellite. HA 2026.10 keeps each
integration’s device record separate. Voice routing uses the ESPHome source
device ID, rather than the scheduler device ID. ESPHome still carries voice and the
existing device entities. Schedule entity state is a device projection;
Home Assistant does not count these timers down.

## Install

1. Build/install the device firmware and controller from this branch using the
   normal EchoMuse update procedure. Firmware build: `cd device && ./compile.sh`.
2. Copy `custom_components/echomuse_schedule/` into HA's
   `/config/custom_components/echomuse_schedule/`.
3. Copy `custom_sentences/en/echomuse_schedule.yaml` into HA's
   `/config/custom_sentences/en/echomuse_schedule.yaml`. Preserve any other
   sentence files; remove conflicting timer/reminder sentence-trigger
   automations or competing timer intent overrides.
4. Restart Home Assistant. In the controller, open the satellite and select
   **Schedules → Home Assistant connection → Generate connection key**.
   Add **EchoMuse Scheduler** in HA's Devices & services, once per satellite.
   Enter the displayed controller URL, connection key, and satellite serial.
   For the HA add-on, the direct URL is `http://<HA-machine-IP>:8768`.
   Do not use the sidebar's `/api/hassio_ingress/...` URL. No controller or
   Home Assistant username/password is needed. Optionally select a TTS entity
   such as `tts.piper`. The satellite must be online with updated firmware.
5. **Custom sentences are the primary voice route and do not use an LLM.**
   Use the Home Assistant conversation agent, or enable **Prefer handling
   commands locally** on a pipeline using an LLM agent so HA tries local
   sentences first. Optionally select **EchoMuse local schedules** in a
   compatible agent's LLM API/control configuration to give the LLM scheduling
   tools for unmatched wording. This is an optional fallback; the custom
   sentence route works without it. Multiple API selection depends on the agent.
6. Clear existing HA Assist timers before enabling the replacement; active
   HA-owned timers are not migrated. New firmware suppresses HA's legacy
   timer capability/events, so install the companion before relying on voice
   timers. Older firmware keeps its existing behavior.

The integration stores the connection key in HA's config entry. The controller
stores only its hash in its persistent database. A key grants access only to
one satellite's schedule snapshots, commands, and audio uploads, including
through the add-on's direct HTTP port. All other add-on routes still require
HA ingress; the key grants no dashboard, shell, configuration or user access.
Use a trusted local network or a verified TLS reverse proxy. Certificate
verification remains enabled. HA 2026.10.1 is the verified API baseline.

Generate a new key to replace the old one, or use **Revoke connection key** to
remove access immediately. Saved alerts continue on the satellite. After key
rotation, use **Reconfigure** on the existing HA integration entry to enter the
new key; it cannot be reassigned to a different satellite. Existing standalone
entries using controller admin credentials remain supported; reconfigure them
to use a scoped key. No firmware update is needed for this authentication fix.

Use the **Schedules** tab beside Config, Console and Updates to manage alerts,
upload sound files, and configure alert volume, snooze, retention and pulse
colors. It uses the controller's login and theme. Date/time inputs are
interpreted in the timezone shown in the form. Recurrence uses that timezone's
calendar. The old separate `/static/schedules.html` page has been removed.

## Local volume commands

With companion version 0.1.7 and the updated sentence file, English commands
such as “volume 50%”, “set the volume to 50 percent”, “volume up” and “decrease
the volume by 10 percent” are handled by HA locally. For an LLM pipeline, enable
**Prefer handling commands locally**. Assign the originating ESPHome satellite
to its room and expose its media player to Assist.

If an exposed media player is playing in that room, HA's original room volume
behavior is preserved. If nothing is playing there, the command targets only
the available, exposed ESPHome media player belonging to the satellite that
heard it. Another room's music does not prevent this fallback. Hidden, offline,
unsupported or ambiguous source players are never replaced by another device.
Explicit device and room targets retain HA's native sentences and handlers.
The Spotify cloud player is not used for the idle satellite fallback; HA's
normal ESPHome service sends volume to the local controller and device.

The companion adds only the missing shorthand sentence. Existing volume
sentences stay owned by HA. Scoped adapters on HA's two volume handlers use
HA's own English grammar to distinguish implicit commands from explicit room
or device targets, then delegate validation and execution to the original
handlers. Other intents stay unchanged, and unloading the companion restores
both original volume handlers. The shorthand still maps to HA's native volume
intent after unload. No firmware change is required for this voice-routing
update.

## Clarification without replacing your LLM

Keep your existing LLM agent and **Prefer handling commands locally** enabled.
Incomplete schedule requests use Home Assistant's native
`assist_satellite.ask_question` action on the originating satellite. EchoMuse
waits for the current pipeline to finish, speaks the question once, and opens
listening without another wake word. The short answer is captured at STT and
parsed locally; it does not replace your conversation agent or install a new one.

For example: “Set an alarm for tomorrow” → “What time?” → “8am”.
“Set an alarm for tomorrow at eight” → “Did you mean AM or PM?” → “PM”.
The original day and recurrence are preserved. “Never mind” cancels the draft.
No item is created until all required details are supplied. Clarification is
limited to four questions and three minutes, and is canceled on integration
unload. A satellite without native question/listen support receives the spoken
question as a normal error instead of starting an unsupported follow-up.

Clock examples include “11.30 a.m.” (STT punctuation), “tomorrow at midday”,
“eight minutes past midnight”, and “quarter to noon”. Replace the companion
integration and sentence YAML and restart HA; no controller/firmware rebuild or
conversation-agent change is needed for this update.

## Voice examples

Bare “stop” uses Home Assistant's native media-pause sentence. The companion
refreshes schedules on the originating satellite and dismisses its active
ringing alert before normal media handling. With no ringing alert, or for an
explicit media request such as “stop the music”, it delegates unchanged to
Home Assistant's original media handler. An alarm on a different satellite
cannot capture the command. A failed dismissal does not also pause music.
Assign the satellite to an HA area: native bare “stop” recognition requires
the source area. Keep **Prefer handling commands locally** enabled when using
an LLM pipeline. There is no global custom “stop” sentence, extra conversation
agent, fake HA timer, or change to local/offline audio playback.

Replace the companion integration and its custom sentence YAML, then restart
HA to apply this change. Updating just the controller add-on cannot remove
the old global sentence from HA's configuration.

Companion 0.1.4 forces an immediate controller refresh before deciding whether
bare stop targets an alert. A debounced refresh could otherwise leave an old
completed-alarm snapshot in the cache. The reproducible check in
`tools/experiments/ha_schedule_conversation.py` starts a real HA 2026.10.1
runtime and exercises its conversation/media services and the local-intent
API used by LLM pipelines, with a simulated HTTP controller and speaker.
It does not verify physical audio, STT/TTS, Spotify playback, or a real LLM.

Companion 0.1.5 and its sentence YAML avoid claiming security commands such
as “arm the alarm” or music searches such as “play alarm” and “play Remind Me
by Royksopp”. Standalone schedule requests use an article (“a five minute
timer”, “an 8am alarm”); alarm requests after “set an alarm” use “for” or a
time introducer such as “at”, “tomorrow”, “in”, or “every”. Temporal clauses
before “remind me” must also start with a time introducer. Explicit “stop the
music” and “stop playing” stay media commands even while an alert rings;
polite generic requests such as “could you stop for me” dismiss that alert.

`tools/experiments/ha_command_regression.py` compares 51 phrases against HA's
native baseline in full-conversation and LLM-local modes, after installation
and while ringing (204 comparisons). It checks real appliance service paths,
the actual Spotify coordinator/player implementation against a simulated
cloud API, both players operating in the same area, schedule controls, failure
handling and full handler restoration.
Phrases that native HA does not handle must continue to fall through rather
than become scheduling commands. Cloud OAuth, real Spotify playback,
microphone/STT/TTS and an external LLM are outside this test's scope.

Confirmations use everyday wording, such as “Your timer is set for 5 minutes”,
“Your alarm is set at 8 AM tomorrow”, and “Snoozed for 9 minutes”.

Companion 0.1.6 understands “Remind me to take the trash out on the 20th at
6 p.m.” The day is the next future occurrence of that date, skipping months
that do not contain it; the reminder message contains only the task. Spoken
ordinals such as “twenty-first” also work. Conflicting dates and days outside
1–31 are rejected instead of silently becoming reminders for today.

“Delete that reminder” selects the last reminder successfully created through
this companion on the originating satellite. That reference lasts until HA
restarts or the integration reloads; without a reference, a single pending
reminder can still be selected. “Delete my reminder for 6pm today” matches both
the time and the local date. Several matches require a more specific name or
time. Replace `custom_components/echomuse_schedule` and restart HA to install
these fixes; no sentence YAML, controller, or satellite binary changes are needed.

- “Set a timer for five minutes.” “Set a pasta timer for 12 minutes.”
- “Set an hour and a half timer.” “How long is left on the pasta timer?”
- “Wake me up at seven tomorrow morning.”
- “Set an alarm for every weekday at 7am.”
- “Remind me to take medicine in five minutes.”
- “In two hours remind me to call Sam.”
- “Remind me tomorrow at 9am to take the bins out.”
- “Remind me to water plants every Monday and Thursday at 8pm.”
- “Remind me to stretch every two hours.”
- “List my reminders.” “Read my reminders aloud.” “What is my next alarm?”
- “Cancel the pasta timer.” “Cancel my next alarm.” “Cancel all timers.”
- “Stop ringing.” “Snooze for ten minutes.” “Give me five more minutes.”
- “Pause my timer.” “Resume my timer.” “Add two minutes to my timer.”

English only initially. “At seven” without AM/PM or a morning/evening qualifier
asks for clarification; repeat the full command with the missing information.
A bare “in five minutes” has no item type and is not accepted as a new
schedule. Duplicate names also need a more specific command. Recognized
commands that fail validation are answered locally, rather than handed to an
LLM that might invent success. Unsupported calendar expressions are rejected.

Custom sentence matching runs on the HA host after STT, using HassIL without
an LLM. STT and TTS location depend on the selected pipeline. All eight HA
built-in timer handlers are adapted for enrolled EchoMuse devices, so a
built-in sentence match also reaches the device scheduler. Unrelated devices
retain their original handlers. Commands are bound to HA's originating device
identity; names, rooms, and model arguments cannot redirect an alert to another
satellite. Typed service calls explicitly select the satellite integration
entry instead.

Reminder text and local sound are saved immediately. The companion then
generates speech with HA and uploads it in the background, preserving the
original deadline. The response explicitly says when speech is pending.
Once cached, that speech is playable offline. If synthesis fails, the device still saves the reminder
with a local sound fallback and the voice response says speech is unavailable.
The companion retries missing reminder speech at most once every five minutes
per item while online. “Read my reminders” speaks the list through Assist.
The controller's standalone reminder form saves text and sound; the companion
will cache its speech when it next sees the reminder.

In the satellite's **Schedules → Alert settings**, choose one global sound each
for timers, alarms and reminders. Each choice applies to all existing and future
alerts of that type on that satellite, including alerts created through HA.
The create-alert form has no per-alert sound option. Legacy `item.sound` metadata
is retained for compatibility but no longer selects playback.
The device announces `local_schedule_sounds`; older firmware shows these sound
controls disabled with an update message, and the API rejects unsupported sound
settings rather than acknowledging an ineffective change.

All three selections initially use **Default**. The timer default remains the
bundled Home Assistant Voice PE sound. Alarms use the stock Echo alarm from
`/system/local/share/earcon/base/system_alerts_melodic_01.mp3`, decoded on the
satellite using its existing system file. This mapping was verified against
`EarconsAudioProvider::alarmDefault()` in the firmware's `libEarconAdapter.so`;
the stock timer uses `system_alerts_melodic_02.mp3`, which is not the current
EchoMuse timer default. If the stock alarm file is absent or undecodable, alarms
fall back to the bundled timer sound. Reminders use the stock Echo reminder
chime (`alerts_notification_03.mp3`) before cached speech. Selecting **Default** restores these
defaults for that alert type.

For **Custom sound**, upload WAV, MP3, OGG, Opus, FLAC, M4A or AAC, then save
settings. Files are converted by controller FFmpeg to 48 kHz mono S16,
verified by SHA-256, and saved on the device. Reminders play their globally selected sound followed by
cached speech. Missing/corrupt custom audio falls back to the
type's default. No remote audio URL is required when an item fires.
Upload limits: 8 MiB input; only the first 43 seconds are used for
converted audio, 32 assets and 64 MiB total device audio storage. Reuploading
identical audio reuses its asset ID. The controller page can remove unused
cached sounds explicitly (`prune_assets`); files referenced by global sound
settings or any saved item are retained. Complete uploads and save settings
before pruning.

## Dashboard and automation

The standard to-do card works with the reminder entity:

```yaml
type: todo-list
entity: todo.echomuse_a_reminders  # replace with the actual entity ID
```

Timer/alarm sensors have lifecycle states (`scheduled`, `paused`, `queued`,
`ringing`, `snoozed`, `disabled`, `waiting_for_time`, `completed`, `missed`).
Attributes contain the stable item ID, UTC `due` timestamp, original duration,
timezone, recurrence, and cached sound/speech IDs. Cards can compute remaining
seconds from `due`; the device does not write flash or HA history each second.
The integration becomes unavailable when the device/controller is unreachable;
an offline list is not replaced with an empty list. Completed one-shot sensors
and their HA registry entries disappear after the retention interval. Cleanup
also works after HA was offline. Recurring schedules remain visible.

Use the HA `echomuse_schedule.command` action with `entry_id` and `command`.
Find the satellite's integration entry ID in HA's integration configuration.
A successful action response includes the acknowledged device snapshot.
Examples:

```yaml
action: echomuse_schedule.command
data:
  entry_id: YOUR_SATELLITE_CONFIG_ENTRY_ID
  command:
    action: create
    item:
      kind: timer
      label: Tea
      duration: 300
```

```yaml
action: echomuse_schedule.command
data:
  entry_id: YOUR_SATELLITE_CONFIG_ENTRY_ID
  command:
    action: create
    item:
      kind: alarm
      label: Weekday wake-up
      due: 0
      timezone: Europe/Warsaw
      rule:
        days: [1, 2, 3, 4, 5] # Sunday=0
        hour: 7
        minute: 0
```

Mutations: `create`, `edit`, `cancel`, `cancel_all` (with `kind`, defaults to
`timer`), `dismiss`, `complete`, `snooze`, `pause`, `resume`, `adjust`, `enable`,
`disable`, and `settings`. Item operations take `id`; `snooze`/`adjust` accept
`seconds`. `edit` takes the full item from the device snapshot. Metadata-only
edits preserve deadlines and ringing state. `list` returns current state.
Optional `revision` rejects stale edits, and `request` deduplicates retries
across device restarts (last 128 requests). Use a stable request ID when retrying
an uncertain mutation; do not generate a new one and accidentally create a
second item.

The controller API has equivalent authenticated endpoints:
`GET /api/schedules`, `POST /api/devices/{serial}/schedules`, and
`POST /api/devices/{serial}/schedule-assets` (raw sound file bytes). IDs are
stable and ownership is checked on both ends. Maximum 64 live/retained items
per satellite; remove completed items or wait for retention if the limit is
reached. Canceled items are removed promptly.

## Alert policy and time

Defaults, editable on the controller page or through `settings`:

| Setting | Default |
| --- | --- |
| Priority | Alarm, timer, reminder; earliest occurrence then item ID breaks ties |
| LED colors | Timer cyan `#00BCD4`, alarm amber `#FF9800`, reminder violet `#9C27B0` |
| Sounds (`settings.sounds`) | Default for each type; custom SHA-256 asset IDs keyed by `timer`, `alarm`, `reminder`; empty string restores Default |
| Pulse cycle | Two seconds, using the existing local firmware pulse renderer |
| Snooze | Nine minutes |
| Volume ramp | Twenty seconds |
| Ring cap | Ten minutes of active ringing; queued items retain their budget |
| Alert gain | 50%, independent of remote playback volume |
| One-shot retention | One hour after firing, while preserving active snoozes/ringing |
| Missed deadline grace | Fifteen minutes |

A higher-priority arrival preempts the sound/LED display and retains the other
item in the queue. Dismiss/snooze immediately selects the next due item.
Controller disconnects, reconnects, remote LED commands, and ordinary TTS flushes
do not stop a local alert. A local wake word immediately ducks ringing alerts,
even with no music playing. Alerts follow the existing music duck depth through
listening, processing and the reply, then fade back to their configured volume.
Unconfirmed wakes release on session close or after five seconds; controller
disconnects also release the duck so offline alerts remain audible. Voice
replies retain full volume, and music is ducked under ringing alerts. Local
alert output is included in the AEC reference.

Calendar recurrence preserves local wall time through DST: missing times move
to the next valid local minute; repeated times fire once at the first occurrence.
Fixed intervals preserve cadence; snoozing one occurrence does not change the
underlying rule. Offline catch-up is bounded rather than replaying every missed
occurrence. Relative timers use Go's monotonic clock during the current boot;
wall-clock corrections update their persisted UTC anchors without changing
remaining duration. Paused timers do not count powered-off time.

Clock confidence can survive a daemon restart within the same Linux boot,
using the kernel boot ID. A marker from an earlier boot cannot establish time.
The Echo has no battery-backed RTC. After a power cut, pending schedules wait
for trustworthy time from the controller or device SNTP. The firmware uses Go's
resolver with `time.cloudflare.com` and `pool.ntp.org` as fallback servers.
Set `ECHOMUSE_NTP_SERVERS` to comma-separated hostnames/IP addresses in the device
daemon environment to use local servers. The recent controller time source
wins. No inbound NTP server is started. A relative timer created before clock
sync can run in that boot, but if rebooted before acquiring a UTC anchor its
elapsed powered-off duration cannot be recovered; cancel/recreate it when the
device reports `waiting_for_time`. A saved date alone is not a time source.

Persistence uses checksummed/versioned snapshots, fsync, atomic rename, and a
previous generation. Corrupt primary data can recover the previous generation;
if neither is usable the scheduler refuses mutations and preserves the files
for repair. Flash failure and exactly-once audible output across arbitrary power
cuts cannot be guaranteed.

## Sharing alerts between satellites

By default an alert belongs to the satellite it was set on. Under **Schedules →
Share with other satellites** you can send a satellite's reminders, timers and
alarms to other satellites, each kind separately and to whichever satellites you
choose. A reminder for the whole house can ring in every room, while timers stay
in the kitchen.

- **Nothing changes until you share.** With nothing shared, schedules, the
  Home Assistant companion and voice commands behave exactly as before.
- **Alerts you already have are shared too**, and new ones, edits and
  cancellations follow. Turning a satellite off removes the shared copies from
  it; the originals stay where they were set.
- **Each satellite holds its own copy**, so a shared alert still rings with the
  controller and Home Assistant offline. A satellite that is offline when you
  change the sharing is brought in line when it reconnects. If the satellite an
  alert came from is offline, its copies keep running.
- **Ask any satellite.** "List my reminders", "what's my next alarm", "stop" and
  the dashboard list work on a satellite that holds shared copies, by voice
  through Home Assistant or the Pipecat flow alike, because a copy is an
  ordinary item in that satellite's schedule. The dashboard marks copies
  "shared" and says where they came from; the original says who it is shared
  with.
- **Stop together** (on by default, one switch for the house). Stopping a ringing
  shared timer or alarm on one satellite stops it on the others, including a copy
  about to ring. Turn it off in a large house if you only want the satellite you
  silenced to stop. Reminders are not stopped together.
- **Cancelling a copy only removes it from that satellite**, and it stays
  removed ("cancel all timers" in one room leaves the other rooms' timers).
  To remove an alert everywhere, cancel the original. Editing a copy on its own
  satellite is overwritten by the original on the next change.
- **Reminder speech travels with the copy.** The controller keeps recently
  uploaded sounds (up to 64 MiB in `schedule_assets/` beside the database) so
  the spoken text reaches the other satellites. A reminder whose speech is no
  longer cached plays the reminder chime there until the original is next
  edited. Global alert sounds, volumes and colours are not shared.
- **Limits are per satellite.** Copies count towards the 64-item limit; a full
  satellite is reported under the panel and the others still get theirs.
- **Home Assistant companion 0.1.11** leaves copies out of the reminder
  to-do list, the timer/alarm sensors and speech caching, so a shared reminder
  appears once, under the satellite it was set on. Replace
  `custom_components/echomuse_schedule` and restart Home Assistant; older
  versions still work but list a copy on both satellites.

Sharing needs no firmware change. It is stored in the controller database and
the API is `GET`/`PUT /api/schedule-sharing` (admin to change).

## Design notes

Why the Echo owns the schedule: HA's timer manager keeps its countdowns in
memory on the HA host, and the controller's own timer registry is cleared when a
device disconnects, so neither can ring with HA or the controller stopped. The
device holds the store, the cached audio and the ringing; HA and the
controller are a projection of it and a way to send commands. A cache on the
controller or in HA is never restored over a newer device snapshot. The legacy
HA timer event path is switched off for a device announcing `local_schedule`
(it also stops advertising `VoiceAssistantFeature.TIMERS`) and stays as it was
for older firmware. Existing HA timers are not migrated; clear them before
enabling this.

Why the built-in timer intents are replaced, not just out-sentenced: HA matches
custom sentences ahead of built-in ones, but that does not cover every
phrase a built-in timer sentence or an LLM tool call can produce, and any of
those reaching `HassStartTimer` would create an HA-owned timer on a
satellite whose timers are device-owned. The companion therefore wraps all
eight timer intents for enrolled satellites, delegates unchanged for every
other device, fails with an explicit error for an enrolled satellite that is
offline, and restores the originals on its final unload. Another integration
replacing the same handlers raises a repair issue instead of silently
breaking replacement. HA has no alarm or reminder intent, so those are new.

Why the entities are the companion's: ESPHome's native API has no list entity,
so the reminder list is an HA `todo` entity owned by the companion. For
timer/alarm sensors the native route was probed (see below): unsolicited
entity metadata is not discovered live, and rediscovery needs the voice
connection to reconnect, which should not happen on every schedule change. So
the companion creates them. HA 2026.10 devices belong to a single config entry,
so the companion's device links to the ESPHome one with `via_device_id`, and
voice commands carry the ESPHome source id.

Routing is by registered device identity, never room name, IP or last active
speaker. Commands carry request ids (128 remembered for deduplication) and an
optional expected revision, and a success is acknowledged only after it is
durable. Limits: 64 live items, 3650 days, 32 cached sounds, 64 MiB of audio, a
4 MiB asset. Simultaneous due items queue; a higher-priority arrival preempts
the display without costing the queued item its ring budget.

How the device loop runs: it sleeps until the next due time instead of
polling. Timers use monotonic deadlines and wake exactly. An alarm or reminder
waits for a wall-clock second, so it re-plans when the firmware steps the
clock or trusts it (`clock.Changed`), and otherwise every second when due
within two minutes and every ten seconds beyond that, which bounds the error from
a clock change nothing announced. A command wakes the loop at once. While anything
rings the loop wakes every 100ms; otherwise it wakes for a due item, for the
two-second state report while connected, and at most once a minute.

Out of scope for v1: voice control with no HA or controller (speech-to-text and
intent matching still run there), speech synthesis on the Echo (reminder speech is
synthesised by HA's TTS when the reminder is saved and cached on the device),
an RTC, and exactly-once audible output across a power cut. A reminder saved
while TTS is unavailable keeps the local chime and says so.

## Compatibility probe and validation

`tools/experiments/esphome_dynamic_entities.py` connects EchoMuse's native
framing to real `aioesphomeapi==46.6.0`, the version pinned by HA 2026.10.1.
The probe observed a new entity's unsolicited metadata on the socket, while
the original listing and normal state subscription did not discover it.
Explicit rediscovery returned the new entity; reconnect reflected removal.
HA's `esphome/manager.py` requests metadata on connection. The companion route
avoids forcing that voice connection to reconnect for every schedule change.
This probe is not a full HA ESPHome runtime test or a claim that controlled
rediscovery cannot be implemented.

Reproducible checks:

```bash
# Separate HA environment, Python 3.14 compatible with this HA release
python3.14 -m venv /tmp/em-ha-tests
/tmp/em-ha-tests/bin/pip install homeassistant==2026.10.1 aioesphomeapi==46.6.0 \
  hassil home-assistant-intents pytest mutagen==1.48.1 ha-ffmpeg==3.2.2
/tmp/em-ha-tests/bin/python -m pytest custom_components/echomuse_schedule/tests
/tmp/em-ha-tests/bin/python tools/experiments/esphome_dynamic_entities.py

cd device
go test -race ./internal/schedule ./internal/clock ./internal/server \
  ./internal/bindings/speaker ./internal/client
go vet ./internal/schedule ./internal/clock ./internal/server \
  ./internal/bindings/speaker ./internal/client
./compile.sh
```

Physical Dot validation remains necessary: actual loudness, light colors,
button hold timing, power-cycle recovery with your network's time source, and
music/Assist interaction under device load. No physical device was available
in this development workspace.

## Reminder announcements and clarification

Reminders play the Echo firmware's `alerts_notification_03.mp3` chime, then
“Here's your reminder” and the reminder text. The firmware file is read from
`/system/local/share/earcon/base/`; it is not redistributed. If unavailable,
the bundled alert sound is used. Existing cached speech is refreshed by the HA
companion while the reminder is still pending.

Under **Schedules → Alert settings**, enable **Repeat reminders** and set
**Additional reminder repetitions** (1–10). Disabled means one announcement;
two additional repetitions means three chime-and-speech passes in total.
These settings apply to all existing and future reminders on that satellite.
Reminders finish automatically after the last pass; dismissal cancels all
remaining repetitions. The maximum ring time still caps playback.

“Set a reminder for the 26th of October” asks locally for a time and reminder
text. Named month dates support an optional year; without one they use the
next occurrence of that date. Music stays ducked through the whole local
clarification, including processing between questions. A short repeated bare
“stop” after dismissing an alert is absorbed for eight seconds; explicit
“stop the music” still controls music. Punctuation such as “stop!” is accepted.

Install the updated firmware, controller, companion (0.1.8), and sentence file
together, then restart HA to load the local grammar and handlers.
