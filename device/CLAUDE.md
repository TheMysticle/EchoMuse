# CLAUDE.md — `device/`

The Go binary that runs on the Echo Dot. Project-wide direction, the
device/controller compatibility rules, the wire protocol and the release
scheme are in the repo-root `CLAUDE.md`; the controller half is in
`controller/CLAUDE.md`.

The why behind these rules — incidents, measurements, worked examples — is in
`docs/history/device.md` under the same headings. Read the matching section
there before changing that area.

## Building the device binary

FireOS 5 (API 22) needs the custom compiler image; plain Go cross-compilation won't work.

```bash
git submodule update --init                       # GoTinyAlsa: the wilbowes fork, NOT upstream Binozo
cd device && docker build -t echomuse-compiler compiler/
./compile.sh                                      # → build/server
go test ./...                                     # host, pure-Go logic only
```

- **GoTinyAlsa stays on the fork** until upstream has its playback fix
  (WriteFrames blocks signals around `pcm_write`, #707) — else a
  signal-interrupted write silently drops the rest of the buffer (clicks).
- **The compiler base (`compiler/Dockerfile`, Go 1.24.0, NDK 21.4.7075529) is
  pinned by DIGEST and must stay so**: `release.yml` rebuilds it on every tag,
  and a drifted compiler shows first as a binary the hardware refuses to run,
  unrecoverable from the dashboard. **Moving the pin needs a real device in the
  loop** — CI never exercises this image.
- `compile.sh` embeds the version via `-ldflags "-X .../client.Version=..."`;
  dirty trees get `YYYYMMDD-HHMM-dev`. Controller tests: root `CLAUDE.md`.
- **Release**: a `v*` tag runs `release.yml`. **Tag with `git tag -a
  --cleanup=verbatim`** — the annotation is the release body the dashboard shows
  beside an update, and the default cleanup deletes every `#` line. Write it for
  someone deciding whether to push firmware to a device they depend on.

## Device audio pipeline

Music rides `0x04`/`0x05` into its own buffer and is mixed against voice at the
ALSA write (ducking: `controller/CLAUDE.md` → "Ducking"). Each mic buffer:

```
raw frame (the board's MicArray: biscuit 9ch S24_3LE, Dot 3 4ch S32_LE) → beamformer (a mic selector) + fixed mic gain (micGainDb, applied to 24-bit samples) → mono S16_LE → [AEC] → [AGC] → [VAD gate] → /data WebSocket
```

- **Batches are 160ms (2560 samples)**, not 32ms periods: `GetAudioStream`
  reads the whole ALSA buffer (512 × 5). Anything assuming 512-sample buffers
  must handle multiples (see `aec.Process`).
- **The always-on wake stream (`mic_start` without `lock_mic`) is ungated and
  AGC-free**, sent continuously in 80ms frames. VAD gate and AGC apply only to
  `lock_mic` turns, with a fresh `ResetAGC()` per stream.
- **Beamformer** (`internal/beamformer/`): picks the perimeter mic with the
  highest onset energy ratio at turn start and locks. `extractChannel` applies
  `micGainDb` (+24dB) BEFORE truncation to S16 (speech is ~−70dBFS);
  `vadThreshold` stays in pre-gain units. **It is a selector — do not propose
  delay-and-sum** (72mm aperture; derivation in SETUP.md). Superdirective is
  reopened (Wil, 2026-10-14), judged on wake word and commands at normal voice
  in household noise; only noisy-room captures can rank beams, and until they
  do the selector stays.
- **AEC** (`internal/aec/`, vendored SpeexDSP-1.2.1), whole mic path incl. the
  wake stream; default off (`aecEnabled`).
  - **Hardware reference (#385)**: ch8 is an in-frame loopback of our playback
    (+33 samples, inverted, pre-DAC); no ring, `aecDelayMs` or governor.
    Promoted only when BIT-EXACT ZERO while silent AND carrying audio while
    playing (`noteEchoRef`) — never on energy alone, which would cancel against
    a real mic. One-way. `EM_AEC_HW_REF=off|on` overrides (env var: a board
    property).
  - **One filter per microphone (#814)**: `aec.ProcessMicsWithRef` runs all
    seven every period, idle ones resting after 256ms of silent reference
    (`quietHold`); one saved file, `aec_echo_path.v2.bin`, all or nothing.
    Hardware reference only. `ProcessWithRef` is for `tools/aec_replay` and
    tests only.
  - **De-aliased (#975) — never "simplify" back to raw ch8**, which aliases
    content above 8kHz and passes every test that plays nothing up there.
    `dealias.go` matches ch8 into our 48kHz output and low-passes that; no match
    → raw ch8. Search runs on a copy outside the speaker lock, backs off after
    three misses, allocates nothing once locked. Armed, mics extract 6dB under
    `micGainDb` and get it back after (`aecHeadroom`).
  - **No volume scalar on the reference** (volume is in software before both
    taps; a scalar applies it twice). **Unity gain on reference extraction**,
    pinned by test (gain clips it and teaches a distorted path). Never raise the
    DAC above unity (127): the loopback stops matching a clipping DAC.
  - **Software tap** (no loopback): `aecDelayMs` **keep 0** (higher is
    non-causal). Capture stalls lose whole batches; the occupancy governor trims
    the backlog **without resetting the filter**.
  - `[aec] far:` logs only while `aecEnabled`; read `mean` beside `rms`
    (`mean≈±rms` is a DC offset, not audio).
- **Barge-in** (controller `_barge_watcher`): device `speaker_flush` drains and
  discards until EOS. `bargeInThreshold` (0.05–0.10) sits below `owwThreshold`
  by design (echo depresses speech-over-TTS scores). **A barge must abort HA's
  run before starting the interrupting turn** (`controller/CLAUDE.md`).
- **AGC** (`internal/processor/`): lock_mic only, release frozen in silence.
  Noise suppression is controller-side (`em_ns.py`, `nsAsr`).
- **VAD** (lock_mic only) on pre-NS/AGC audio: opens after `VAD_SPEECH_MS`,
  closes after `VAD_SILENCE_MS`, then an end-of-speech sentinel.

### The speaker's voicing (stock sound, #229, 2026-10)

- **Firmware announcing `stock_sound` always voices its speaker; no key turns
  it off.** The voicing is board data (`board.Speaker.Voicing`,
  `pkg/board/voicing.go`) measured on that board's driver; a board without one
  plays the plain chain and does not announce `stock_sound`.
- Order, on device with no plug: volume → user EQ → correction → bass guard +
  three upper compressor bands → limiter (`speaker.refreshStock`). **The law is
  on only with a voicing** (`stockLawOn`; a nil voicing panics). With a plug in
  or a controller processing the audio, the plain chain and law stay.
- Law `StockVolumeDb` (0.5dB/level + `CeilingDb`, applied in float before the
  chain); the button tone follows it (`CueVolumeGain`, capped at unity).
  Correction keeps its 80Hz high-pass and 65Hz notch; `VoiceFilter` is replies
  only. Upper bands need a smoothed attack and a hold (instant = grit).
- **The bass guard is held on** whatever `bassGuardEnabled` says, and the
  dashboard hides it for `stock_sound`. EQ presets are relative to the voicing;
  changing a preset's values strands its users at "Custom".
- Python (`em_eq`, `em_mbc`, `em_volume`) is the reference; Go is bit-exact to
  `outchain/testdata` — regenerate with `gen_vectors.py` after changing either.
  Stock's files and captures stay off the repo (Amazon's).

## Key Go packages

| Package | Role |
|---------|------|
| `cmd/server.go`; `internal/client/` | Entry point; `/control` and `/data` clients |
| `internal/server/`; `internal/config/` | Mute/volume/LED state; runtime config (env defaults, controller push wins) |
| `internal/bindings/` | Hardware drivers; each opens what `pkg/board` resolved, never a number |
| `pkg/board/` | Board identity (idme `device_type_id`), parts BY NAME, `Resolve`, emOS thermal tuning. `guard_test.go` fails on hardware numbers anywhere else |
| `internal/wakeword/` | Pure-Go openWakeWord feature pipeline behind `Inferer`; golden fixture `testdata/` (`gen_fixture.py`) |
| `internal/wakeword/ort/` | ONNX Runtime, **dlopen'd, never linked**, so a device without it boots. `DefaultOptions` (1 thread, XNNPACK, no spinning) is measured — more threads raise CPU |
| `internal/wakeword/shadow/` | On-device scoring; `Push` never blocks |
| `internal/wakeword/fixture/` | One tolerance policy for the host test and `tools/oww_probe`; tolerances relative to the tensor's scale |
| `internal/listen/` | Private-listening gate and `Resolve`; pure, clock-injected |
| `internal/bindings/als/` | Ambient light by name, TSL2540 (i2c `als_lux`) or TSL2584 (IIO `illuminance0_input`) per batch, tried in order (`board.LightSensors`), never a loose `tsl` match. **Never read `calibrated_lux`** (kernel corruption; test-guarded); `illuminance0_input` only with an idme `alscal` `ams_0_0` baseline, else `unsafe`. Never `unbind` it (hangs). **Poll 5s, not 1s** (dark reads flood ram_console, the only crash log). `Lux()` is nil, never 0. `Report()` → `ambient_light_status`, whole bus enumerated before matching |
| `internal/bindings/jack/` | `/sys/class/switch/h2w`, polled; `Watch` dispatches its STARTING state too (a boot has no edge) |
| `internal/outchain/` | EQ → bass guard → limiter on the mix at the ALSA write, bit-exact to `testdata/gen_vectors.py`. Inactive until the ack carries `output_chain` (never runs twice). No `%` per sample: 32-bit ARM Go has no divide |
| `internal/sendspin/` | Sendspin player (#89): pull scheduler (`SetMusicSource`), consulted only while `0x04` is empty. **Speaks aiosendspin 9.1.1's wire** (§6.4 of `docs/audio-states.md`); re-run `tools/sendspin_interop/run.sh` before trusting an MA upgrade. `internal/firewall` opens 8928 before `Start` and closes it on every stop (emOS drops inbound). `OutputClock` gates readings over 3ms off prediction |
| `internal/wifi/` | WiFi change with rollback; reload is `svc wifi disable/enable` ONLY. **SSIDs are 0–32 BYTES** (`ssid.go`): `ssid_hex`, byte compare, quoted or hex. Never in a shell command |
| `internal/bluetooth/` | HCI passive scan over `/dev/stpbt` (Bluedroid `pm disable`d first); `emit.go` filters |
| `pkg/led/`, `pkg/mic/`, `pkg/speaker/`, `pkg/buttons/` | Hardware interfaces |

## Boards: hardware is found by name (#541, 2026-10-04)

`docs/boards.md` is the porter's guide; these must stay true. Parts are named
in `pkg/board/hardware.go`, found once by `board.Resolve`, opened via
`board.CurrentLayout()`. Registered: biscuit, radar (Echo gen 2 — never "Dot
3"), donut (Echo Dot 3rd gen).

- **Only biscuit has a numeric FALLBACK; a new board never gets one.** It is
  logged and sent once per start as a `[board]` warning (`Layout.Problems`).
- **An unidentified device gets biscuit's layout incl. gpio444**, so ADD a
  board before running firmware on it (on the Dot 3 that pin is an audio clock).
- **Ambiguous is refused, never first-match. Inputs match by NAME, never by key
  capability. The Bluetooth node is stated, never probed.**
- **Board differences are data in `Hardware`, never a branch in a driver**
  (`TestNothingBranchesOnBoardIdentity`). Fixture:
  `pkg/board/testdata/biscuit-fireos5/`.
- **`server board`** prints the layout and changes nothing — run it from a temp
  path beside the live server before installing a build that changes this.
- **Radar** = biscuit + its own speaker data: `AmpMute` (`MFP Gpio Mute`,
  asserted by the driver at every open/close); `Filter`, read at start-up from
  the unit's own `audio_device.xml`, never compiled in (zeros = silence;
  values are Amazon's); `Setup` (`DRC Control` Disabled); `BassGuard`
  70Hz/−25dB.
- **Start-up is amp, DAC, then mute released once RUNNING** (~1.25s ramp);
  `Ignore Ramp Up` clicks.
- **Capture is a `MicArray`**: channels, format, edge mics in steering order
  with angle, centre or −1, loopback or −1. Mics are channels 0..NumMics−1
  (test-held); `beamformer.New(array)`, `aec.New(nMics)`; no centre → wake on
  the first edge mic.
- **Donut** (4ch S32_LE, no centre mic): **`Loopback` (`DL1_AWB_Record`) must be
  open AND delivering before the mics open**, or the TAS2770 loses its clock
  until reboot; the binding waits for audio, retries 4s, and a stated loopback
  the kernel lacks means NO mics. `MicMutes` = ADC_A, ADC_B. `RingLED0` 240
  biscuit / 195 donut (`ringLEDFor`). idme `miccal` not applied.
- Still biscuit's inside drivers: amp switch, DAC unity, jack tables, `stop
  <service>` names.

## Private listening (the default since 2026-09-21)

Spec: `docs/listening.md`. Under `owwOnDevice=on`, with a controller announcing
`listen_session` and a scorer loaded, the device is `local`: **nothing is
sent** until a crossing opens a `listen.Gate` session, which sends `0x07` frames
from `listen.WakeLead` (260ms) before the crossing until `listen_close` or a
device limit. `listen.Resolve` decides the state; `syncListenState` reports
`listen_state` on every config push and connect.

- **The limits are the device's**: no `listen_ack` in 3s, 30s max, mute,
  `StopMic`, data-link drop. No controller failure may leave an Echo streaming.
- **Missing scorer is `degraded`, never `stream`.** The button still works.
- **One timestamp per frame**, given to both `PushBytesAt` and `Gate.Push`;
  crossings carry CAPTURE time.
- **`WakeLead` is counted in FRAMES back from the crossing frame**, not time;
  the pre-lead part of the first frame is silenced on a copy (shared buffer).
- A crossing inside an open session is words — `Gate.Open` refuses.
- **`mic_stop` ends only a lock_mic turn**, never local listening or a session
  (sessions end by id via `listen_close`). `mic_start` with lock_mic REPLACES
  the wake stream.
- **The barge bar applies to music too** (`speakerPlaying`), needs two
  consecutive frames, and is **dated from the FIRST**.
- **The Echo cuts its own reply at a barge crossing** (`listen.CutReplyOnBarge`)
  only while `replyStillHeard`. Do not drop reply audio from the session.
- Without `listen_session` the state is `stream` in every mode.

## On-device wake word (shadow mode)

- `owwOnDevice` = `off`/`shadow`/`on`; **an unknown value is `off` at BOTH
  ends**, never guessed.
- **`on` needs `oww_trigger`, separate from `oww_shadow`**; without it
  `em_shadow.effective_mode` degrades `on` to `shadow`, never to `on`.

### `on` — the device decides, the controller keeps watching

- Device sends `oww_wake` (score, threshold cleared, age — never a timestamp)
  → `Device.pending_wake`, acted on at the next mic frame where turn setup
  lives. `em_shadow.decide_wake_source` is the pure decision.
- **The controller keeps scoring without triggering** and records agreement
  (`turns.ctrl_wake_score`/`ctrl_wake_delta_ms`). Barge-in stays controller-side.
- **`last_wake_mono` is the CROSSING instant, never arrival.**
- **Mute is checked on the device** (`onWakeCrossing`); the crossing is still
  reported. A pending wake expires after `MAX_PENDING_WAKE_S` (4s).
- Label `wakeword-dev(score)` keeps the `wakeword` prefix readers match.
- **Arbitration compares capture times**: `capturedMono` and ping `mono` mapped
  via `em_listen.DeviceClock`, never arrival − age − RTT/2; controller-scored
  wakes via `CaptureClock`. A claim within the winner's window cedes; the
  winner holds window + 3s; a grant is never revoked.

**Shadow mode scores and reports, never acts**, on the byte-identical 80ms
frames sent to the wire.

- **Inference never on the mic goroutine**; `Push` hands off and the scorer
  **drops and counts** frames when behind.
- **Nothing per frame**: crossings immediately, the rest on the ~30s stats tick
  with `maxInferMs`/`maxGapMs` — **maxima, not averages**, `atomic.Int64`, gap
  measured in `enqueue`, and the first frame records NO gap.
- **No WALL-CLOCK timestamps from the device**: ages, or the MONOTONIC clock.
- **Thresholds must match**: `SetBargeThreshold` applies the lower bar while
  `PcmSpeaker.VoiceAudible(wakeword.ScoreSpan)` (not `IsStreaming`), and never
  raises it. `turns.wake_threshold` is the EFFECTIVE threshold; the rollup has
  agreed / missed / **not_comparable**.
- Correlation (`em_shadow.ShadowTracker`) at turn-persist time: nearest crossing
  within `MATCH_WINDOW_S` (2.0s) wins and is **consumed**. `turns.dev_shadow`
  = was it scoring; `wake_counters.dev_*` = crossings with no turn.
- Assets at `shadow.DefaultDir` (`/data/local/share/echomuse/oww`,
  `EM_OWW_DIR`), **never in the firmware** (would double the OTA). Absence is
  ordinary, logged once. `device/tools/oww_probe` checks parity and CPU.
  **Enable on one device at a time** (~23% of a core with
  `embedding_stream.onnx`, which falls back to the stock embedding).
- **Re-read the scorer PER FRAME, never cache it for a stream**, and `Close()`
  signals `quit`, never closes `ch` — either alone leaves a device deaf or
  panicking.
- **Bouncing the HA connection (`update_oww_model`) flaps EVERY entity** and
  fires state-triggered automations on the event entity (user fix `not_from:
  [unavailable, unknown]`). Think before adding a bounce.
- **Install-before-switch, never a fallback**: `em_oww_sync.hold_back_model`
  holds `owwModel` back until the classifier is on the device;
  `_install_then_switch` pushes the real config after. Never stand a device
  down to controller scoring meanwhile (silent override; streaming on private
  firmware). Only locally-scoring devices are held; old AND new mode are
  consulted; the install runs in the background; tests pin ordering,
  capability gate and failed-install-returns.
- **A missing model never changes the mode**, offline case included:
  `effective_mode` takes no readiness, and a test pins that the reconcile never
  assigns the mode.
- **Every device carries all four stock classifiers**
  (`em_oww_assets.STOCK_MODELS`, test-pinned both ways to `dashboard.jsx`
  `WW_MODELS`; `timer`/`weather` are intents, excluded). `desired_assets`
  (`include_stock` True) serves both transports. **`CLASSIFIER_SLOTS` budgets
  leftover CUSTOM classifiers only**; stock is never evictable.
- **Reconcile-on-connect** (`em_oww_sync.reconcile` via
  `em_reconcile.on_connect`): failure to LOOK is not absence; repair, never
  switch; full set whatever the mode (`reconcile_action`); quiet when nothing
  to do; **an install is followed by the config** (`push_config`); presence by
  **md5, not filename**. `missing_selected_classifier` = deaf (warn, repair,
  re-push); `missing_assets` = incomplete (repair quietly at info).

### Asset distribution (`em_oww_assets.py`)

`em_oww_assets` plans (pure); `em_api` executes over the wizard (USB/ADB,
**mandatory step**) or the shell plane.

- ARM runtime **vendored in the controller image**, pinned by AAR sha256
  (`onnxruntime-android` 1.19.2); models from openwakeword or `oww_models/`.
- **md5 is the only success**: push to `.part`, rename on match.
- **Four classifier slots, LRU by device mtime**; the selected model is pinned;
  only positively recognised classifiers are deleted.
- Free space checked against what needs sending, via `parse_free_mb`, never an
  awk field (busybox wraps).
- **`TransferResult` is truthy, not a list** — never assign it over an
  accumulator. Pinned by test.
- `DEVICE_DIR`, model names and the stem rule are test-pinned to the firmware.
- **`silero_vad.onnx`** (speech gate, `internal/client/speechgate.go`) is OUR
  typed-tensor copy (`controller/tools/silero_typed.py`, Dockerfile `silero`
  stage, sha256-pinned), never openwakeword's, which SIGBUSes ORT on armv7 and
  **presents as a hang** — look for `BUS_ADRALN` in `logcat`. Optional, never
  evictable, RMS gate without it.

## The external audio jack

- **`Init()` runs `stop media` and waits on the speaker substream before
  opening** (mediaserver holds it with a headset in; ALSA's open blocks
  forever). Opens anyway on timeout, logging the holder. **Never make `stop
  media` permanent** — mediaserver keeps the HAL, DSP and I2S clock alive; we
  win the race once per start. Hang tell: no `PcmSpeaker initialised`.
- **The speaker is card 0 device 23, the mic 24**; `pcm0p` is Android's.
- **`SetJackRouting` applies BOTH positions and stays idempotent**: amp
  switch, `HP Driver Gain Volume` (ctl 62, accdet drops it to 0), and
  `Audio_DacMux_Setting` On with a plug (in the 30s reconcile). `Right Channel
  Only` and `Ignore Ramp Up` deliberately not copied. Android's `AudioOut_2`
  also reconfigures codec state on insert.
- **Routing is PHYSICAL** (jack contacts): build no mux/destination layer.
  Level is ours.
- **Unresolved (#117, #141): a plug degrades the whole audio subsystem.** Read
  #117 and JOURNAL 2026-08-12 first; codec, SoC and mixer state are IDENTICAL
  audible vs silent — **do not look there again**. Open: whether
  `Ext_Speaker_Amp_Switch` gates the internal driver; only a **positive**
  `skew` shows lost audio (healthy devices drift negative).
- **No stereo**: the device duplicates L=R; the downmix is the controller's.
- `tinymix` and the codec regmap (`/sys/kernel/debug/regmap/2-0018/registers`)
  exist. **Never `tinyplay` while the server runs** (wedges the device).
- **MIXER CONTROL IDS ARE NOT STABLE ACROSS KERNELS — resolve by NAME, and let
  an absent name be loud.** All writes go through `internal/bindings/mixer`
  (`mixer_get_ctl_by_name`); `guard_test.go` fails on
  `exec.Command("tinymix"`; `start_server.sh` uses names
  (`controller/tests/test_mixer_names.py`). **Call only functions both
  devices' `libtinyalsa.so` export** (check with `llvm-nm -D`) — a missing
  symbol is a crash-loop and A/B rollback. `device/tools/mixer_probe` reads
  names through the same path. The mic ADCs have no regmap: prove capture by
  signal.

## The BLE proxy, and what it costs the device running it

Passive HCI scan forwarded to the controller as a second ESPHome device (recon
and cadence: `controller/CLAUDE.md`). **Scanning costs its own Echo's WiFi.**

### The LE scan costs the WiFi link, and the scan YIELDS for it (2026-09-23)

- Settled, do not repeat: the chip ignores scan interval/window (320/30 is just
  `esp32_ble_tracker`'s default); cost is proportional to scan time; vendor
  init, power save and 2.4GHz don't help — **do not rebuild the kernel for
  power save**. `coex_wmt_ant_mode=1` is right.
- **`Scanner.Yield`**, polled every 100ms in `cmd/server.go` (never edge-set),
  stops the scan for a button turn, a listening session, a voice reply arriving
  (`PcmSpeaker.VoiceArriving`, self-ending 2s after the last period) and any
  shell session. `/dev/stpbt` stays open; the silence watchdog is disarmed
  while yielded. A controller-scoring always-on stream is not a turn.
- **Music gets bursts (`bluetooth.MusicDuty`) sized from the buffer, never the
  clock** — a learned reserve; the music wins. Sendspin declares
  `bluetooth.MusicHeadroom` while the proxy is on. Tests: never dry, no gap past
  8s. Log `[ble] no scan for 8s of music` is the one to read.
- **Adverts go on `/data` as `0x06` only when the ack announced
  `ble_adverts_data`**, else the control message, which the controller keeps
  accepting forever.
- In `DataClient.SendBleAdverts`: **an unsendable batch is DROPPED**, never sent
  on the control plane; **nothing is sent during a BOUNDED TURN — the turn, not
  `micActive`** (`advertsYieldToTurn`). The plane is chosen **per batch**.

### The emission gate (`emit.go`) — derived from what HA reads, not from taste

Target ~one advert per device per second. Forward on: a new payload from a
**known** address; RSSI ±3dB (≤1 per 250ms); 1s of nothing for that payload;
global 30s ceiling. **A first sighting is NOT an arrival** (addresses rotate);
an early flush **resets the ticker**. Never `filter_duplicates=1` at the chip
(it throws away the RSSI Bermuda reads).

### The HCI transport resets, unresolved as of 2026-09-01

A mic stall comes first and WiFi fails BEFORE our `/dev/stpbt` reopen, so
`em_ble_proxy`'s "reopening re-initialises the radio" warning is a hypothesis
— fix that wording. **Gate memory pressure is RULED OUT.** The gate correlates
with no known mechanism; do not call it the cause.

## CPU topology, thermals and why `cpuPct` lies

- Quad-core A53; HPS (`/proc/hps/`) parks idle cores, so one CPU in
  `/proc/cpuinfo` is a power state. **`num_base_perf_serv` (floor) is raised to
  2 every start (`applyCoreFloor`)**. **Never write `cpu1/online`** — HPS
  silently re-parks it.
- **emOS board tuning**: `server platform-init` (once per boot,
  `start_server.sh`, emOS only) applies stock's thermal/hotplug values. Board
  identified POSITIVELY by idme `device_type_id`; every zone and cooler resolved
  before ANY write, else none; every value read back; unknown board keeps kernel
  defaults. The script greps for `EM_PLATFORM_INIT_V1` first (an old binary
  would start a second server). `thermal_budget` levels: written 0-based, read
  1-based.
- **`cpuPct` is a share of ONLINE capacity** — read it with `coresOnline`.
  `thermalCoreLimit` below `coresTotal` is the earliest throttling signal;
  `maxTempC` is the hottest of all zones.

**Which goroutine is the load? `cpuprofile.req` (`internal/cpuprofile`, 2026-10-17).**
Every thread of the server is named `server`, so `top -H` can show that the
process is busy but not what with. `echo 30 > /data/local/etc/echomuse/cpuprofile.req`
makes the running server write a Go CPU profile of the next 30 seconds (1-120)
to `/data/local/tmp/cpu.pprof`, then removes the request. Same shape as
`capture.req`: no setting, no controller change, one stat a second when idle.
Pull and read it on the workstation, with the matching `build/server` so lines
resolve (function names are already inside the profile):

```bash
adb pull /data/local/tmp/cpu.pprof .          # or via the shell plane
go tool pprof -top -nodecount=30 cpu.pprof    # who burns the cycles
go tool pprof -top -cum cpu.pprof             # by call tree, including callees
go tool pprof -http=:8080 build/server cpu.pprof   # flame graph
```

Profile in the state being investigated (idle, music, a turn) and keep the run
to what you ask about: sampling at 100Hz has a cost of its own while it runs.
A profile counts CPU time over wall time, so read percentages against the
core count in `coresOnline` (see above), not against 100%.

## Finding the controller when multicast stops (2026-10-26)

**An Echo can stop hearing multicast while staying associated, with every
unicast path working.** Seen on a Radar under emOS: after a firmware restart
its mDNS browse for `_emcontroller._tcp` failed every round for 8 minutes
while the controller answered each query within a second (watched from
another host), the Echo's own Spotify engine answered no mDNS query, and the
group rekeys since boot had all completed in dmesg. Spotify kept working,
because it is driven through Spotify's servers. `wpa_cli reassociate` (a fresh
4-way handshake) and the next browse found the controller in a second; the
known fix until then was a power cut. The cause is not found.

Two things now cover it. **The last controller address is persisted**
(`internal/client/lastcontroller.go`, `/data/local/etc/echomuse/last_controller.json`),
so the reconnect fast path that skips mDNS also works on the first dial
after a restart or an OTA; it is only an address, and the TLS credentials and
token decide what the link may do, as for one mDNS returned. **And three failed
browses in a row while associated make the Echo reassociate** (`wifi.RejoinIfDeaf`,
wired through `discovery.OnMiss`), then at most once per 15 minutes, never
during a WiFi change and never on FireOS, where the framework owns the
supplicant. Anything else that needs multicast while the link is up (Music
Assistant finding the Echo, a phone's Spotify Connect discovery) is not
covered by either.

## Volume / mute persistence

- **Volume is software (`PcmSpeaker.SetVolume`, after the chain, ramped one
  period); the DAC sits at unity 127** and only mutes around amp/stream
  changes. 0.5dB/step law, unity 127, level 0 silent; starts from 100 via
  `SetVolumeApply`; silent until told.
- **Never above codec unity**: `DEVICE_VOLUME_MAX`/`volumeMax` = 127 (pinned),
  conversion in **one** place (`em_volume.py`). `Ext_Amp_Gain` (ctl 13) is inert;
  `HP Driver Gain Volume` (ctl 62) is live but a taste call.
- Buttons step 4dB over `volumeButtonFloor` (47)…127; explicit `Set()` is not
  floored (HA 0.0 = silent); a press from below lands on the floor.
- `volumeButtonSound`: only on a button-made change after 100ms of quiet, never
  for HA sets; `VolumeCue` gets `speaker.VolumeGain(newLevel)` baked in once,
  bypasses EQ/chain; Up at ceiling replays, Down at floor is silent; presses
  replace the in-flight cue. Dashboard gates on `volume_cue`, not `wake_cue`.
- **Volume is state, not a setting**: no dashboard control,
  `em_config_sections.STATE_KEYS`, read-only on Status.
- **Volume persists controller-side** (`startupVolume`), restored by
  `Server.SeedVolume` on the **first config push per run only**; the
  connect-time `volume_state` is suppressed until seeded or changed locally.
- **Mute is device-sovereign** in `/data/local/etc/echomuse/state.json`,
  restored pre-connect. **The write is synced**: temp, `Sync`, rename, sync
  the directory — rename-only lost mute across a power cut. Unreadable boots
  unmuted. `sendspin/store.go`, `aec/persist.go`, `config/report.go` stay
  unsynced on purpose.

## LED priority system

- Scenes (`em_scenes.py`, `ledScene`). With `led_anim` the device **animates
  locally** (`internal/server/animator.go`). `meter` reads the **voice plane
  before the music mix**; `meter*` → `resolveMeter`, which clamps itself.
  `ttlSec`: 30s listening, 135s spinner (**coupled to `_fetch_tts_audio`'s 60s
  ×2 — move one, move the other**), `em_scenes.meter_ttl` per response; it is
  the dead-man. A newer spec or `leds` frame replaces atomically (generation
  counter). `listening: true` on listening frames drives the direction overlay.
  Mute red and volume cyan are device-local.
- Outcomes by rhythm, not colour, from `device.last_turn_outcome` (set in
  `_persist_turn` **and `_record_dropped_turn`**, consumed by
  `_leds_turn_end`); **`no_ha` alone uses colour** (orange), after a 600ms
  listening hold.
- **Start a device-local pulse ONCE per state** (`pulseKind`); phase from
  elapsed time (`pulsePhase`).
- Playback ring clears on `playback_stats` (`device.playback_done`), never a
  wall-clock estimate.
- System LEDs beat the direction arc (`ledMode`); suppressed paints still record
  `baseLEDs`.
  - **The ring belongs to the LINK STATE; mute yields to it** (`linkDown`,
    `suppressPaint`, truth-tabled).
  - **Action and volume buttons are inert while `linkDown`**, gated in `cmd`,
    not the evdev binding. **MUTE stays live** (hardware; inert would hand back
    a live mic).
  - **A 5s action hold asks to pair, before that gate** (`client.PairHold` →
    `StartPairing`); the closing release is swallowed; the 2-min window closes
    early when the credential files change.
  - **Three link rings, kept three**: orange pulse = no controller; orange
    alternating = refused (`errRefused`), fixed by holding the button; white
    pulse = pending. `held` survives redials.
  - **FireOS 6: the mute LED is Amazon's `amz_privacy`** (gpio444, boots
    unmuted, can enter privacy but never leave). `led/privacy.go` finds it by
    name; `server/privacy.go` reconciles after every press and at boot, always
    to muted. **Never read `power_button_state`** (blocks the console).
  - **Mute ring is device-sovereign**: controller writes recorded, not painted.
  - **Volume arc** holds 2s against animations, not against an action press
    (`CancelVolumeDisplay()`, no repaint); expiry → `paintBaseLEDs`. Physical
    presses only. Mute LED is sysfs gpio444 active-high, not Amazon's gpio445
    (`mute_button.go`).

## Where the serial comes from

`GetSerialNo` (and emOS `init.c`, same order): **`/proc/idme/serial`**, then
`getprop ro.serialno` (FireOS only), then `androidboot.serialno` on the cmdline
(only before Android's init strips it). **idme leads: the cmdline truncates on
FireOS 6's 32-bit kernel and is the only source that can be WRONG** — its use is
logged. **Non-printable ASCII is rejected** (`unknown-device` beats a wrong
key). `emos/init/serialcheck.c` covers both parsers. `/proc/idme` also has
`board_id`, `product_name`, `productid`, `device_type_id`, `alscal`,
`miccal.0`–`6`.

## The emOS console password

`consolePassword` is written to `/data/local/etc/echomuse/console.pw`
(`config.WriteConsolePassword`), never checked; emOS's init enforces it so the
console works without the firmware. Never undo:

- **A POINTER** — empty means "no password" and must reach the device (as
  `DuckDb`).
- **Written only when the content changes** (eMMC wear).
- **From the config handler in `control.go`**, not `OnConfigApplied`.
- **Temp file + rename** — a torn record reads as NO password.

Record `<iterations>:<salt hex>:<hash hex>`, hashed by the controller; design
in `controller/CLAUDE.md`.

## On-device Spotify Connect (`device/spotify/`, Rust)

**The Echo fetches its own Spotify audio.** `em-spotify` is librespot 0.8.0
as a library, wrapped so the Go firmware drives it over a unix socket and
takes 48kHz S16LE periods on a second one — which is exactly what
`PcmSpeaker.MusicSource` wants, so ducking, the output chain and the volume
law all apply with no new plane and no new frame type.

It is device-side because the controller-fed path pays for every link
stall: this fleet measures 4.6–7.1% loss, TCP's ordering turns one lost
segment into a multi-second application stall, and `em_player`'s 4s pacing
lead is described in its own source as "MITIGATION, not a cure". librespot's
prefetch sits between the network and the speaker instead.

**Proven on hardware 2026-10-10**, Bedroom (Radar, emOS, FireOS 6 32-bit
kernel), with `em-spotify-probe`: the static binary executes, DNS resolves
through emOS's `/etc/resolv.conf` in 3ms, and rustls completes a TLS 1.3
handshake to `apresolve.spotify.com` (AES-256-GCM, 200 OK, 156ms total).
Those were the three risks and all three are retired.

Six things that are load-bearing, each of which fails silently otherwise:

- **musl, not the NDK, and therefore emOS only.** bionic resolves through
  Android's property service, which emOS does not provide, so an NDK build
  has no DNS here; emOS's init writes `/etc/resolv.conf` from the default
  gateway and musl reads it. This is INVERTED on FireOS, which has no such
  file and whose `/etc` is a symlink into read-only `/system/etc` — so the
  feature is gated on `base_os: emos` and a FireOS device keeps
  controller-fed music. `emos/tools/build-busybox.sh` reached the same
  conclusion for the same reason, and its static musl busybox is the
  evidence such a binary runs at all.
- **ET_EXEC, not a static PIE.** Rust's musl targets can emit a PIE, which
  this kernel would have to self-relocate; build-busybox.sh passes `-no-pie`
  and calls ET_EXEC "the shape already proven on these devices".
  `compile.sh` asserts no INTERP, ET_EXEC and ARM with the same three
  readelf checks, and `em_spotify_assets.describe_binary` re-checks them
  controller-side before an upload is stored — a PIE has no PT_INTERP, so
  without that check it would pass as "static".
- **librespot's rate is a compile-time constant** (`SAMPLE_RATE: u32 =
  44100`, no runtime option anywhere in the crate), so the sink resamples
  160/147 to 48k. Exact ratio, integer phase, no drift.
- **The mixer attenuates nothing.** The Echo applies volume in its own write
  loop and on Radar does so AHEAD of the output chain, so a second gain law
  here would play at the square of the intended volume and select the wrong
  loudness curve. `accept_device_level` stores a level that came FROM the
  device without echoing it back, or the two ends chase each other.
- **A free account kills the process.** `Session::check_catalogue` calls
  `exit(1)` when the access point pushes user attributes — a second or so
  after a successful login, with no hook to intercept. The daemon reads the
  account type itself and reports `premium_required` cleanly; the firmware
  must match librespot's own stderr line as the backstop for the race where
  librespot exits first.
- **The engine is NOT in the firmware OTA.** ~4MB would double every
  update's payload across both A/B slots whether or not Spotify changed —
  the reasoning that keeps the ONNX runtime out. It is uploaded on the
  dashboard's Integrations tab and pushed over the shell plane, md5-verified,
  into `/data/local/share/echomuse/spotify`. That directory must be
  `mkdir -p`'d first: the transfer probes that the destination directory
  exists and nothing else creates this one.

Build with `device/spotify/compile.sh` (pinned Rust cross image, by digest).
Pure modules test on the host with no librespot in the graph:
`cargo test --lib --no-default-features --target x86_64-unknown-linux-gnu`.

## cgo dependency

SpeexDSP C source (AEC) is vendored in `device/internal/aec/`. `internal/bindings/mixer` links the device's own `libtinyalsa.so` (see the mixer section for the symbol rule). The compiler Docker image provides the ARM cross-toolchain. If adding new cgo dependencies, they must compile cleanly with the `echomuse-compiler` image against the FireOS 5 sysroot.

## This fork: wake beam, Pryon, endpointer, Radar stock tuning

This fork's own rules, kept verbatim through the 2026-11-05 upstream merge
(upstream condensed this file; these describe work upstream does not have).

**Radar's stock tuning inside upstream's stock-sound profile (merged
2026-11-05).** A board that names `Speaker.StockTuning` (radar:
`/system/vendor/etc/audio-algorithms`) has stock's own FIR, ParametricEQ, MBCL
and OutputTrim read off the device (`outchain.LoadRadarTuning`). When they
load, `outchain.NewWithTuning` runs them IN PLACE of the voicing whenever
StockSound is in force (`Chain.tuned`), in `ProcessWide`, under stock's mixer
law (the speaker sets the voicing's `CeilingDb` to 0, `HasStockTuning`).
`eqStockCurve` still switches FIR/PEQ/trim; the MBCL follows `bassGuardEnabled`
and its floor, its limiter follows `limiterEnabled` at its own settings. A plug
in the jack turns the profile off (`chainForJack`), so neither runs. Without
the files a radar plays upstream's fitted `radarVoicing`. There is no
`TakesVolume`/`SetVolumeGain` any more: the stock profile already applies the
volume ahead of the chain. Bit-exact to the Python on `radar_*` vectors, which
need `ECHOMUSE_RADAR_TUNING` (a Radar's audio-algorithms; never in the repo).

- **Beamformer** (`internal/beamformer/`) — selects the perimeter mic with the highest onset energy ratio (fast/slow EWMA) at voice turn start, then locks for the duration. Its `extractChannel` also applies the fixed mic gain (`micGainDb`, default +24dB) against the full 24-bit sample before quantising to S16 — captured speech sits at ~−70dBFS, so gain must happen pre-truncation to recover real resolution. `vadThreshold` stays in pre-gain units (the device scales it by the gain internally). **It is a selector, not a summing beamformer, and that is settled — do not propose delay-and-sum.** A frequency-domain implementation (exact FFT phase shifts, no interpolation artefacts) exists in `device/tools/bf_capture` and was measured as only marginally better than mic selection. The reason is the 72mm aperture, not the code: diffuse-field noise coherence is 0.84–0.99 below 1.5kHz where speech energy lives, so a sum has almost nothing uncorrelated to cancel, and 36mm adjacent spacing puts spatial aliasing at 4.76kHz — a working window of roughly 2–4.7kHz. Superdirective/differential beamforming is the only class that works at this aperture and it trades against white-noise gain (20dB+ amplification of sensor self-noise) on unmatched capsules across four ADCs. Full derivation and the coherence table are in SETUP.md's mic-array section (SETUP.md is the architecture reference; the chronological log is JOURNAL.md, the rooting prerequisites docs/rooting.md). **An experimental, default-off beam exists for the WAKE path only** (`wakeBeam`, `internal/beamformer/fbf.go`, announced as `wake_beam`): eight fixed SUPERDIRECTIVE beams over ch0, 1, 3 and 4 (the four Radar's stock AFE sums), MERGED THE WAY STOCK'S `GroupBeamMergerV2` DOES (2026-10-12, read from `AFE.cfg` and `libasp.so`): every beam is scored by speech-band SNR (400-2500Hz power, fast 0.75 / slow 0.987, over a noise floor that is the slow average for 5s and the minimum of a ~4s buffer after), the eight overlapping groups of three adjacent beams ([0 1 2] .. [7 0 1]) are scored by their beams' mean SNR, a group takes over when 1.26x better than the current one after a 96ms hold, and the output is the MEAN of the group's three beams plus stock's `ASR Output Gain` of 8.3dB, with a soft knee at 0.75 of full scale so a raised voice rounds off rather than rails. The first version picked ONE beam by an onset ratio, which engaged on any creak, dropped to the centre mic after a second and measured 2.5-4dB BELOW the centre mic for a quiet distant talker (`tools/bf_design -sel`); the group scheme measures +5 to +6dB level and +4 to +6dB SNR over the centre mic in the same scene, with and without a TV at the talker's level or double it. The centre mic carries the path only for the first 3s. Locked turns are untouched. It is there to be A/B'd on a device against the single mic, not because the argument above changed, and the argument is about DELAY-AND-SUM: `tools/bf_design` measures a sum on these four mics at -4.6/-4.1dB SNR against a side interferer where the shipped beams give +0.1/+4.9, because stock's beams are minimum-variance designs with negative white-noise gain (-7dB at 250Hz) behind a per-unit mic calibration, and ours are the same kind. **The shipped beams match stock's to within 0.15dB of mean directivity at 250/500/1k/2k/3kHz** (4.40/5.57/6.78/7.53/6.44 against 4.49/5.64/6.75/7.52/6.59) and to about 1dB per beam in white-noise gain; the look directions are stock's four pairs, not an even grid, and the noise model is a diffuse field plus an interference ring. They are formed in the SPECTRAL domain, one complex weight per mic per 62.5Hz bin applied inside the canceller stage, not as time-domain filters: 64-tap FIRs were needed to reach stock's 250Hz directivity (2.8dB at 32 taps) and cost 2.8ms a batch where the weights cost 0.6, with no fit to correct. It applies `/proc/idme/miccal.*` as a Q14 gain (16384 = unity, read NUL-terminated; direction confirmed on a device) and designs its weights in the background when switched on, a few seconds on an A53. A second, independent setting (`wakeBeamAra`, `wake_beam_ara`, `internal/beamformer/ara.go`) adds an adaptive interference canceller after the beams: each beam's interference is predicted from the two beams 135 degrees round the ring (stock's `ARA_V2` reference table, and the reason the look directions are in stock's index order) per 62.5Hz band and subtracted. In `tools/bf_design -ara` it takes the whole path to +8.7/+9.5dB SNR in a room against a TV 6dB over the talker (the beams alone: -2.9/-1.0) for 0.5-0.9dB of the talker (up to 1.6dB against a weak interferer, where it gains only +2 to +3dB), and does no harm with the TV at the talker; it needs seconds of the interferer to learn. **`tools/bf_design -sel -sel.tonal` measures it in ONE run** (talker and TV as tone combs in the same canceller bins, each read at its own frequencies), because the two-run subtraction `-sel` uses is invalid once the canceller adapts differently with and without the talker: through the group path ARA adds +2.3dB SNR over the beams against a TV as loud as the talker and +7.2dB against one 2.5x as loud, for about 2dB of talker level, and +1.9dB with the TV in the talker's direction (no harm). Tones are easier to cancel than music, so read these as an upper bound. Stock ships NO ARA or noise-floor state (no files, training time 0, signal and noise power both start at 4e7 so SNR starts at 1): it learns from blank on every boot, as ours does. The stage's spectral part always runs when the beam is on, so the path has 256 samples (16ms) of latency whether or not it cancels and the neutral output and every beam line up. It holds its filters when its own residual jumps, because the references being louder than the target beam does not mean the talker is absent. A third independent setting (`wakeBeamTurns`, `wake_beam_turns`, default off) makes a locked voice turn listen through the same beam output instead of the single perimeter mic the lock picked, starting on the group engaged at the wake (or, with none, the one nearest the lock's mic) and then CARRYING ON SELECTING as before the wake (2026-10-12: stock's libasp does nothing on a wake word, since the WW timestamps it receives feed only its spatial features, localisation and SNR diagnostics, and its ASR stream is one beamformed channel throughout; the first version froze the choice, which a talker who moves away from the wake's group would have paid for), with an 80Hz high-pass like stock's input stage that runs in a turn only. The turn still runs our AGC and speech gate after the beam, which stock's ASR path does not have (its AGC is VoIP-only); it is how stock works (one beamformed, cancelled ASR channel serves the wake word and the recognition: `AFE.cfg`'s ASR path, `channel_type: beamformed` in the Pryon scorer config), and the default is untouched. The beams colour an off-axis talker's spectrum by under a dB within 15 degrees and about 2dB at worst (`tools/bf_design -color`); whether that moves transcript accuracy has not been measured, which is why it is a setting. **Raw capture, for what the saved recordings cannot say** (`internal/client/capture.go`, `tools/bf_design -raw`, 2026-10-13). A command's saved WAV is the path's OUTPUT, so a loss in it could be one capsule, the geometry the beams assume, the group selection or ARA. On the Echo, `echo 20 > /data/local/etc/echomuse/capture.req` makes the mic loop write the next 20s (1-60) of the raw 9-channel S24_3LE it already reads to `/data/local/tmp/capture.raw`, then removes the request: no setting, no controller change, and it costs a stat a second when idle. `bf_design -raw capture.raw [-raw.from s -raw.to s]` prints each microphone's band levels against the centre mic (a dull capsule shows) and the wake path in four modes (beam, beam+ARA, with and without Radar's rules) with the groups it used. Prompted by recordings in which the voice alone (the TV's tail subtracted) had its 1.6-3.2kHz band 21dB under its 200-400Hz band through the beam with ARA OFF, against 8dB for the single mic, with a TV on; in a quiet room with no TV the same path was normal (-7.0dB), so the loss belongs to steering or selection with a TV present, not to the beams or ARA alone, and the simulator (which predicts -3dB) does not reproduce it.
  **One switch, 2026-10-13.** `wakeBeam` now turns on the whole set: the beam, `wakeBeamAra`'s canceller, `wakeBeamStock`'s rules and `wakeBeamTurns`' beam for the command; the three keys are still accepted (a controller may push them) and no longer read (`client/data.go`). Why: `tools/bf_design -sweep` runs every combination of them, twice over each real capture so stock's merger is measured warm, and no piece wins alone. Voice over background, TV capture / quiet capture: centre mic +10.4 / +27.2; beam with ARA off +9.0 / +33.1 (loses to the TV); our rules with ARA on +12.2 / +29.1 (loses in the quiet); stock rules with ARA on, 2.5kHz, shaped +12.3 / +31.0, which is the only row that is never the worst. **Stock's own files were measured and not adopted**, because the `/vendor/etc/audio-algorithms` copy is on every device and was worth trying: its beam table ties our design on voice over background (+0.6 / +1.9dB) and is duller (spectral tilt 3 to 6dB worse); running it in stock's own 768-tap filter bank with its four taps applied (`-bank`) gives the same as folding the taps; its two EVD tables are worse than either on these four mics (DI 3.9 / 5.9dB at 250Hz / 1kHz against 4.5 / 6.8). `fbf_stocktable.go` stays so those tools can repeat the comparison, and nothing in the firmware reads a vendor file. The beams ship as `fbf_design.bin`, our own design generated at development time (`go test ./internal/beamformer -run TestEmbeddedDesignIsCurrent -regen-design` after changing a design constant; the test fails on a stale file), which is why the beam exists from the first block like stock's. In stock rules the path starts on group 0 and never parks on the centre mic, which stock's ASR path never reads; the centre mic is also about 15dB weaker than the perimeter mics. The dashboard greys `wakeMic`, `beamAngle`, `beamformingEnabled` and the presets while `wakeBeam` is on. `wakeEndpoint` stays its own switch.
  **The Echo's own wake word decoder is an on-device engine you can choose (2026-10-13), `owwModel` = `pryon_alexa`, capability `wake_pryon`.** `internal/wakeword/pryon` loads Radar's `libpryon.so` and its ALEXA package from `/system` at run time (dlopen, like the ONNX runtime; absent means off and the open engine stays), behind the `shadow.Engine` interface the data client and `cmd/server.go` now use for both. It is an opt-in for ONE platform and nothing of Amazon's is in the repository: the calling convention was read off libWakeWordManager and is documented in `tools/pryon_probe/probe.c`, which also runs the real library on a PC (qemu-user + bubblewrap + the NDK, no root) and is how every number here was measured. Measured on a Radar Echo on emOS: 4 of 4 wake words in the TV capture and 3 of 3 in the quiet one, scores identical to the PC's; 30% of one core listening in real time (the open scorer was 36%); 18MB resident. It uses the model's own bars (0.923, 0.864 playing), so `owwThreshold` does not apply; with barge-in on, near misses at the playback bar count, since stock lowers its bar through a client property this engine does not set. The decoder DROPS audio when pushed faster than it decodes, so the scorer drops first (`maxBacklogMs`) and counts it. Its result struct carries keyword, type and sample range but not the score, which is read from the decoder's own log line on the same thread (`ParseScore`). The controller treats the engine as the same word: wherever it has to score itself it uses the open `alexa_v0.1` (`em_oww_models.DEVICE_ONLY`), and an Echo without the capability is held on its current word rather than told to load what it cannot. `go test -tags pryonlive` on an android/arm build runs the real library (`live_test.go`).

**`wakeBeamStock` (`wake_beam_stock`, default off) puts the wake beam on stock's own rules, both decoded from libasp on 2026-10-12.** The merger (`GroupStock`, `stockStep` in `fbf.go`, `tools/bf_design -grp.mode`): each beam has a WIDE-band (300-4500Hz) SNR and floor, smoothed 0.75/0.25; a group scores the sum over its beams of SNR x bias (the loudest floor over the beam's own); the best group gets a vote per frame and the selection moves when one has 125 (1s; the votes accumulate and only clear on a switch); inside the group each beam is weighted by its share of the score (equal shares if the total is under 1e-6). Stock's config 1.26 margin and 12-frame hold are NOT on that path. **With it on, ARA's learnt state is also cleared whenever the speaker starts or stops** (`SetPlaying`, driven by the same `speakerPlaying` the scorer's barge bar uses): stock resets both the canceller and the beamformer in the setter at libasp 0xcdbd8 / 0xa7a38 whenever the playback mode changes, because what the mics hear to be cancelled differs with the speaker on and off, so what was learnt for one is wrong for the other (relevant when the Echo plays music). Decoded and NOT ported, with reasons: the AEC's coupling to ARA's weight decay (`0.987 + 1.2e-9 x (mean 1-4kHz mic power - 1e5)`, capped 1.0, floor 0.985, applied while frozen) is in stock's own power units, which are not ours; the AEC's flag to the beamformer picks between NormalNoise and DiffusedNoise coefficient sets, which are the same file in this firmware (EVD off), so it changes nothing; the DNN VAD's "OutputAttenuation" gain is 1.0, so it gates nothing; ARA also restarts its training state and fully resets every 225000 frames (30 min). In the one-run measurement it is steadier against a TV (SNR gain +12.6 vs +9.1dB, +16.1 vs +11.3dB with a louder TV, worst burst +11.9 vs +7.5dB) and slower to follow a talker who moves (level -2.3 vs +4.1dB over the centre mic) and quieter with the TV and talker in one direction (-7.8dB), which is why it is a switch to A/B and not the default. **ARA's learning control** (`ARAGate`, `tools/bf_design -ara.gate`, and set by the same switch): 0 is ours (references over the target, hold, unexplained-residual freeze), 1 is stock's (each beam's OUTPUT SNR over its minimum-statistics floor scales the step by a logistic gate, about 1 below SNR 3, half at 10, 5% at 20, and ANY beam over the line stops every beam learning), 2 needs both. In the one-run tone measurement they are within 0.5dB of each other in TV scenes, stock's better by 1.2dB SNR gain with TV and talker in the same direction and 1.5dB worse on the talker's level with no TV; **the simulator cannot reproduce the real-room failure** (a faint reverberant voice from another room cut 9dB against 3.6dB of hood), so the default stays ours until a raw capture settles it. Stock's numbers for the merger are decoded too (`memory: radar-stock-ara-internals`), so "cannot be read" below is out of date for them. Stock's own numbers for the canceller and beam merger cannot be read out of the firmware, so the beam-against-beam match is measured and the ARA numbers are ours. **Far-field reach is therefore not a beamforming problem here** — it is room noise floor, distance and placement; the single-channel levers (`nsAsr`, wake model) are the ones that exist

- **Endpointer** (`wakeEndpoint`, `internal/beamformer/endpoint.go`, announced as `wake_endpoint`, default off) — **a wake-triggered command is ended by Home Assistant's VAD, and with a TV on that VAD hears the TV as speech and waits out its own 15s cap** (measured 2026-10-12: sessions of 15s, 15s and 9s against ~2s in a quiet room, with the beam on, off, and the canceller on and off, so it is not the audio path). **Stock has nothing on the Echo to copy here**: its front end cleans the stream and Amazon's cloud decides where the utterance ends. **The first version judged speech by power over the MINIMUM of recent power (stock's canceller floor) and was wrong, found by running it on the three real recordings**: a TV's dialogue swings 5-14dB above its own quiet moments the whole time, so it is "speech" by that measure continuously. What separates the talker is level against the TV's PEAKS: the command is 7-8dB over the 90th percentile of the TV around it. So the reference is the 97th percentile of the speech-band power (400-2500Hz, smoothed 0.75) over the 12s before the wake word (minus the wake word and the command's first 2s), fixed when the command starts so a long sentence cannot raise it; a block is speech from 2.0x it until it falls under 1.4x; the command has STARTED after 160ms of speech past the wake word's own 250ms tail and is OVER after 1.0s with none. `tools/bf_design -ep.clip a.wav,b.wav` runs the firmware's endpointer on recorded sessions (the TV-only part of each standing in for the room before the wake): the three recordings end at 2.2s, 2.3s and 2.2s. It sends the end-of-speech marker a button turn already sends (`0x04`), which the controller already handles in a private session turn (`oww_paused` is set before `_run_voice_locked`, so it lands in `voice_queue`): **no controller change, no protocol move**. It needs 6s of history to have a reference, and without one (`Warm()` false) never ends anything, so a session straight after a restart is left to the controller. **Limit:** a talker no louder than the TV's peaks is not separable by level. Parameters are `EM_EP_QUANTILE`/`EM_EP_ON`/`EM_EP_OFF`/`EM_EP_SPEECH_BLOCKS`/`EM_EP_SILENCE_BLOCKS` on the device, and each session logs `[endpoint]` lines once a second.

| `internal/sendspin/` | Sendspin player (#89, EA): Music Assistant connects to the Echo directly for synchronised multi-room audio. Noise KKpsk2 responder, token pairing, the spec's Kalman time filter, FLAC decode, and a **pull** scheduler: `PcmSpeaker.SetMusicSource` makes the write loop read the substream's ALSA `delay` each period and ask for the audio due when that period reaches the DAC — the `0x04` plane plays in order and has no timestamps, so a push could not place anything. Consulted only while `0x04` is empty (HA wins), and the player reports itself taken while `0x04` has anything. **Speaks aiosendspin 9.1.1's wire, which disagrees with the spec in seven places** — `docs/audio-states.md` §6.4 lists them; re-run `tools/sendspin_interop/run.sh` against any new aiosendspin before trusting a Music Assistant upgrade. Held to references rather than readings: the time filter to aiosendspin's to the microsecond, FLAC bit-exact against its encoder, the token and Sentinel psk_id to the spec's vectors. **It is the first thing on the Echo that LISTENS, and emOS drops inbound** (init's `firewall()`): `internal/firewall` inserts an ACCEPT for 8928 before `Start` advertises and removes it on every stop, so an Echo with Sendspin off stays outbound-only. Without it the player logs "listening" and every connection times out, and Music Assistant does not list the player at all, so it looks like discovery failing. First heard on 15LE (emOS 32-bit) 2026-09-30: play, duck, seek, volume both ways, pause/resume, discovery. 2026-10-01, 15LE + C95 grouped: in sync by ear, 0.1–0.4ms self-reported, corrections 2–7/min settled (bursts to ~300/min for a minute or two), playback cost below what /proc resolves, both players stop in the same second as the controller link and resume with it, and a Music Assistant left/right stereo pair works with the mono player (MA picks the channel before encoding). `[sendspin] playing:` logs the counts once a minute. The occasional crackle was dropped audio, not corrections: tinyalsa's `pcm_write` discards the rest of a signal-interrupted write, fixed in the fork (see the GoTinyAlsa note above). `OutputClock` gates any reading over 3ms from prediction and `pullSource` brackets its status read with two clock reads (#710). **Low-latency streams (2026-10-24, Radar on emOS).** A stream whose chunks arrive with under 300ms of lead (`player.lowLatency`, hysteresis to 400ms; Music Assistant's 2s and Spotify never qualify) makes the speaker hold the ring to 48ms (`throttleRing`, `shallowRingFrames`) and build music in 256-frame pulls (`sourcePeriodBytes`, `lowLatencyPeriodFrames`; 512 until 2026-10-29; voice stays on whole 2048-frame periods, so the FIR in `outchain` takes any block up to `MaxPeriodFrames`), where the blocking write otherwise keeps the ring at 171ms. Lead must exceed the ring's occupancy plus the period being built (~53ms nominal here); 80ms is the recommended lead and 70ms ran 2 hours with one underrun, from a ~650ms Wi-Fi freeze (2026-10-25); 60ms still dropped out. When a period is short, `Fill` waits for decoded audio until the earlier of 40ms and the DAC time minus 8ms (`fillwait.go`, `[sendspin] fill wait:`; 16ms until 9d59355, and `pastTight` counts the periods the smaller reserve saved), without moving the output timestamp. **The reserve, not the ring, is a late packet's margin**: the deadline is measured back from the period's play time, so a packet must arrive 8ms before it is due however deep the ring is, and shrinking the ring buys no tolerance. What a late packet must beat is the play time of the period holding its first sample less the reserve, and that period starts up to one pull before the sample, so the pull length is lead the stream needs: 256-frame pulls halved the underruns of 512 at a 50ms lead on one network spell (2026-10-29, Bedroom against the living-room pair, 26 against 49 and 50 in 16 min). **The 8ms reserve cannot shrink**: `[speaker] write margin:` (once a minute) logs the work from a pull's Fill returning to its write completing, and it peaks at 6-9ms a minute on a Radar with music alone (15.7ms in a minute with a voice reply mixed in, still written 15.6ms ahead). The underruns left at 70-80ms are network-side: the router's 5GHz radio holds delivery to every client on a 5s grid (docs/experiments/sendspin-latency-2026-10-24.md). **Do not tune the ring from the kernel's `Auddrv_DL1_Interrupt_Handler underflow` count.** That line is the vendor IRQ handler logging when the DMA index has not moved, not an underrun: it appears about once a minute in clean playback, and in an earlier 1024-frame experiment it fired ~1.5 times a second and put 1,800 lines in the ~500KB kernel log ring. The real XRUN is `mtk_pcm_I2S0dl1_pointer underflow` (zero in every run here) together with the player's `underruns`, snaps and late drops and the arrival minimum. The 64ms ring of the first version was picked from the IRQ count and was too deep. `[sendspin] arrival:` logs min/median/max lead once a minute; it counts into 1ms buckets because the first version sorted a minute of leads under the player's lock and stalled the speaker, a DL1 underflow every 60s to the second. Most of the lead a laptop streamer loses is in ITS encoder, not here: aiosendspin's libav FLAC encoder holds a frame until it has 4608 samples (96ms at 48kHz, level 5) and stamps it with its first sample, so a 200ms lead arrived as ~105ms. Level 0 with a 480-sample `frame_size` (10ms) fixes it; the Echo's decoder takes any block size |

| `internal/sendspin/leader*.go` | Speaker groups (docs/speaker-groups.md): a Sendspin SERVER inside the firmware, run only when the controller says `groupRole: leader`. `Leader` reads 20 ms periods from the Spotify ring (`spotify.Player.Read`, with `Idle` and `Events` for pause and discard), stamps them on its own monotonic clock, starts `LeaderStartLead` (500 ms) ahead and bursts to `LeaderAhead` (2 s), plays them itself through the same pull scheduler under an identity time filter, and FLAC-encodes once per channel variant (mono is (L+R)/2, left, right) for each `member`'s bounded queue (overflow drops and resyncs, so one slow member never stalls the leader). Members are ordinary `Client` players the leader dials (Noise `KKpsk2`, initiator side, keys from `leaderStore`, one per member, installed by the controller's `group_key`; a member's own copy by `group_trust`). Two refused handshakes drop the key and report `needs_key`. A pause keeps the unplayed audio (replayed from `pauseMargin` before the stop); skip, stop, another source and a different track after a pause discard it. The group engages only when at least one member is ready at rest, otherwise the Echo plays solo exactly as before. **A group ranks under every other playback server** (`rank`, 2026-10-26): the leader marks its `server/hello` with `echomuse_group`, which can only lower a server's standing and so needs no proof, and a member turns it away with `concurrent_attempt` while a TV streamer or Music Assistant holds it, so the leader's 5s retry brings the group back once that server lets go. At equal rank the two took the Echo from each other every 5s. The Echo's own Spotify turns away only a group (`SetOwnMusic`); any other server's audio takes the music slot from Spotify (`musicMux.Active`), and `spotifyPoll` pauses Spotify without resuming it, which on a leader stops the group. `musicMux` tests one atomic pointer (`groupLeader()`), so an Echo that is not leading has no leader, goroutine, port or file. Not measured on hardware: sync accuracy, CPU and behaviour on a lossy link |

**On Radar the volume goes IN FRONT of the output chain, not after it**
(`outchain.Chain.TakesVolume`, 2026-10-10). Stock attenuates in AudioFlinger,
before the AFE's FIR and MBCL ever see the signal, so MBCL's compressors only
engage once the user has turned it up — at an ordinary volume the music sits
under band 2's −18dB threshold and the stock curve's +10dB of bass passes
untouched. Applied after the chain, as every board did, those compressors see
full-scale audio at every volume: porting MBCL's bands 2–4 (8239c66) then held
the bass down at levels nobody was listening at, measured as bass-over-mids
falling from +12.9dB to +5.9dB at every volume against a stock model's +16.5dB
(level 80) to +1.7dB (level 127). The chain ramps the gain per period exactly
as `softVolume` does, and the speaker then settles `softVolume` instead of
applying it, so nothing is attenuated twice. Only while the chain is ACTIVE:
an inactive chain (the controller still processing) keeps the volume after,
as before. Biscuit is unchanged — its single bass band was tuned in place.
The stock FIR is then chosen BY that volume, as `AFE.cfg` chooses among
Radar's five `EQ_*.cfg` curves (controller/CLAUDE.md, the output chain).

Turn-state ring colours (listening ring, thinking spinner) come from **LED scenes** (`em_scenes.py`), configurable per device (`ledScene` + custom colours). Firmware with the `led_anim` capability (v2.9+) **animates locally**: the controller sends one `led_anim` message per state change ({pattern: solid|spin|rotate|pulse|meter|off, colors, periodMs, ttlSec}) and the device renders frames on its own ticker (`internal/server/animator.go`) — controller/WiFi jitter can't judder the ring. `meter` throbs with the live speaker RMS (tapped at the ALSA write, so it tracks audible audio, not the ~5.5s-ahead send) — measured on the **voice plane only, before the music mix**, unlike the AEC far-end tap which deliberately sees the mixed output; a meter fed the mix throbs to the music bed before the response has started; its response curve is config-tunable (`meter*` keys → `AnimSpec` pointer fields → `resolveMeter`, which clamps independently of the dashboard ranges) because it is a taste parameter that needs iterating in a real room, not a firmware OTA per pass. `ttlSec` is bounded per phase — 30s listening, 135s spinner (**coupled to `_fetch_tts_audio`'s 60s timeout ×2 attempts, since the spinner spans HA think time AND the fetch — move one and move the other**), and computed per response for `meter` via `em_scenes.meter_ttl` so a long TTS cannot self-clear mid-answer. Loss-resilience: newer spec or raw `leds` frame atomically replaces the animation (generation counter), and `ttlSec` is a dead-man that self-clears the ring if the controller dies mid-turn. Legacy firmware falls back to controller-streamed frames. Controller `leds` messages carry an explicit `listening: true` flag on listening-ring frames — the device's direction overlay keys off it (pre-scene firmware inferred "listening" from an all-green ring, which breaks for any other scene; the heuristic remains as fallback for old controllers). The direction overlay brightens the base ring colour instead of painting green. **Where the marker points** (2026-10-19): with `wakeBeam` on, from the wake beams' own estimators (`wakeBeam.trackDirection`, `WakeBeamDirection`): each beam that hears speech (SNR over 3, where stock's ARA gate has fallen to ~1) pulls with its power OVER ITS FLOOR, smoothed ~400ms, so it follows a moving talker and ignores a steady TV; weighting by SNR instead read a talker at 160 degrees over a TV at 225 as 116, because the beams facing the TV have a high floor. With nobody heard for 3s there is no direction and no marker (a wrong one is worse). With the beam off it is still the per-mic HF-energy argmax in `Process`, which follows whichever mic is loudest NOW rather than the mic `Lock()` chose and is erratic. It runs on numbers `step` already has, so the audio path is untouched. `ringSector` adds 15 degrees of hysteresis so a talker on the edge between two LEDs does not flicker. Mute ring (red) and volume arc (cyan) are device-local and scene-independent by design.
