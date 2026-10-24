# Sendspin latency investigation, 2026-10-24

Device: Radar G090XXXXXXXXXXXX, emOS, USB serial console. Sender:
`/home/Mysticle/sendspin-streamer`, aiosendspin 9.1.1, desktop audio monitor,
48 kHz, 20 ms capture/submission batches and 480-sample (10 ms) FLAC frames.
The starting firmware was `20261024-0423-dev`, with the uncommitted
1024-frame pull changes on top of commit `22061bf`.

## What the latency number means

The streamer computes `(play_us - server.clock.now_us()) / 1000` after
`commit_audio()`. This is remaining scheduled lead, not a measurement from
physical input to acoustic output. `parec` buffering, accumulation of the
20 ms capture block, and output processing are not included. Synthetic tones
also produce a block in advance, unlike a microphone capturing that block.
Use loopback/correlation to claim an end-to-end latency.

Receiver queue-plus-pull requirements, before transport jitter or scheduling:

| Ring target | Pull frames | Nominal requirement |
|---|---:|---:|
| 64 ms | 2048 | 106.67 ms |
| 64 ms | 1024 | 85.33 ms |
| 64 ms | 512 | 74.67 ms |
| 48 ms | 512 | 58.67 ms |

Voice still takes whole 2048-frame blocks. With the 48 ms ring that path
requires roughly 90.67 ms. These changes do not establish sub-80 ms operation
during simultaneous voice playback.

## The IRQ log is not an authoritative underrun count

The original 64 ms floor was selected from occurrences of
`Auddrv_DL1_Interrupt_Handler underflow`. That log can be a false positive.
In the [vendor IRQ handler](https://github.com/amazon-oss/android_kernel_amazon_mt8163/blob/3e46726afcbe2972733296cbcbee36a4c0adf60f/sound/soc/mediatek/mt_soc_audio_8163_amzn/mt_soc_afe_control.c#L3359),
`HW_memory_index > u4DMAReadIdx` selects the non-wrapped case. Equality goes
through `bufferSize + current - previous`, treating no movement as consumption
of the entire ring. A recent pointer/status read can leave these indices equal.
That branch logs without consuming or resetting the ring.

The [PCM pointer callback](https://github.com/amazon-oss/android_kernel_amazon_mt8163/blob/3e46726afcbe2972733296cbcbee36a4c0adf60f/sound/soc/mediatek/mt_soc_audio_8163_amzn/mt_soc_pcm_dl1_i2s0Dl1.c#L468)
uses `>=`, and its underrun path logs `mtk_pcm_I2S0dl1_pointer underflow`, clears
the driver's remaining-data count, and returns `SNDRV_PCM_POS_XRUN`.
This is vendor source evidence, not a disassembly proving the exact running
kernel matches every line. It is sufficient to reject counting IRQ messages
as one audible click each. The live baseline accumulated thousands of IRQ
messages while the pointer-underflow count remained zero.

Measure pointer-underflow/XRUN events, player underruns/snaps/late drops,
arrival-lead minima, and audible/recorded continuity together. Do not clear
dmesg; compare counts and timestamps between samples, accounting for log wrap.
Use explicit busybox applets: the emOS shell's built-in `tail` did not honor
our requested line count.

```sh
dmesg | busybox grep -c 'mtk_pcm_I2S0dl1_pointer underflow'
busybox grep '\[sendspin\]' /tmp/server.log | busybox tail -12
```

## Changes and experiments

- Preserve the 4 x 2048 hardware ring. An 8 x 1024 experiment failed to prepare
  the PCM and the supervisor rolled back after three fast exits. It is reverted.
- Use 512-frame pulls for low-lead Sendspin without voice. Preserve the duck
  rate, FIR history and drift correction across block sizes.
- Keep the shallow policy through an empty decoded queue until stream clear/end.
  Previously `lowLatency()` required `active()`, the mux surrendered ownership,
  and silence refilled the hardware ring. Returning packets then had to drain
  that silence, and whole/short block transitions reset the output clock.
  Spotify can still take ownership if Sendspin is empty and Spotify is active.
- Apply shallow pacing before the active check, and use the chosen output block
  length for the alert duck ramp even when the source yields silence.
- With 64 ms ring / 512-frame pulls / 79 ms requested lead, a one-minute report
  reached 18 player underruns. This has insufficient arrival-jitter margin.
- The candidate uses a 48 ms ring / 512-frame pulls. The maximum initial drain wait is
  140 ms: a full 8192-frame ring needs 122.67 ms to reach 48 ms.

The shorter pulls run the fixed-size FIR FFT more frequently. CPU and scheduling
headroom need checking on the actual device, especially with voice/wake activity.
Host tests cannot establish hardware or Wi-Fi timing margins.

## Live soak result

The user reported clean-sounding playback and requested ten minutes unchanged.
The test retained the existing 20 ms sender batches and 79 ms configured lead;
the UI showed approximately 81 ms remaining lead. No sender source code was
changed. Firmware: `20261024-0515-dev`; binary SHA-256:
`25720c56ef6358f30de0b51ddc710f7936f4e750b88b689a36d9269bd1966a3a`.

| Counter | 13:37:14 baseline | 13:47:14 (10 min) | 13:51:15 (extended) |
|---|---:|---:|---:|
| Player underruns | 2 | 3 | 5 |
| Resync snaps | 3 | 4 | 6 |
| Entire chunks dropped late | 16 | 17 | 17 |
| Small drift corrections | 10 | 60 | 79 |

There was **one new player underrun in the selected ten-minute interval**, and
three across the extended approximately fourteen-minute interval. There were
**zero PCM pointer-underflow messages**; the final dmesg still retained boot,
so the observation was not invalidated by its ring overwriting the interval.
Every captured DAC summary reported zero resets. Small drift corrections are
normal clock tracking, not underruns.

The ten-minute arrival summaries had a median of 78 ms and a minimum of 44 ms.
The underrun increment in the 13:43 report coincided with that 44 ms minimum;
normal minutes were substantially better. The earlier two player underruns
preceded the measurement window and coincided with a 36 ms minimum. This supports
arrival jitter as a remaining limitation; it does not identify whether each
outlier originated in capture, transport, or receiver scheduling.

Conclusion: a substantial reduction in required lead, and clean to the user's
ears, but **not a zero-dropout guarantee** or a demonstrated sub-80 ms physical
capture-to-sound delay. Leave 79 ms running for evaluation. No OTA is necessary
for this connected device: it is running the candidate in slot B, with the
original firmware retained in slot A. The prior B binary is also backed up as
`/data/local/bin/server_b.before-latency`.

Evidence: [raw final snapshot](sendspin-latency-2026-10-24-soak.txt) and
[minute counters](sendspin-latency-2026-10-24-soak.json).

Validation: race-enabled Go tests for `internal/sendspin`,
`internal/bindings/speaker`, and `internal/outchain`; targeted `go vet`; the
FireOS/ARM Docker cross-build. Hardware tests cover this Radar's streaming
path, not Spotify, grouped playback or simultaneous voice output. The next
sender-side improvement to investigate is 10 ms capture batches to match its
already-10 ms FLAC frames, with physical loopback measurement before claiming
an end-to-end result.

## Follow-up: wait for data before its existing playback deadline

The 48 ms hardware queue is not merely fixed latency: it is also time during
which a late packet can still arrive. Previously Fill padded a partially
available period with silence immediately. That discards this remaining
opportunity even when the DAC will not reach the missing samples for tens of
milliseconds.

The follow-up build (`20261024-1357-dev`) keeps the same ring, pull length and
sender. On a low-latency shortage only, it waits for decoded audio until the
earlier of 24 ms from now and the measured DAC time minus 24 ms of processing /
scheduler reserve and the measurement uncertainty. Arrival and stream-clear
notifications wake the wait; the player mutex is released while waiting.
The original output timestamp is retained. It does not move playback later,
reschedule the stream, or conceal an underrun if the deadline expires.

The availability check and actual Fill share the same sample-drop/repeat/startup
plan. This matters around drift corrections and partial periods: counting only
raw queued frames would either wait unnecessarily or still underrun after a
positive correction. Clear generations prevent a waiter from leaking old-stream
counters into a new stream.

New once-a-minute `fill wait` telemetry reports waits, recovered waits, timeouts,
and maximum elapsed wait. The first test build recorded one timeout during
initial synchronization; compare steady-state deltas rather than calling that
a playback dropout. Race-enabled tests cover partial and empty queues, exact
sample/timestamp preservation, deadlines, cancellation, startup silence and drift
correction budgets. The timeout test explicitly verifies that a real missing
period still increments the underrun counter.


## Milestone verification after extended playback

The USB-connected Radar still runs `20261024-1357-dev`, SHA-256
`3bf0a45f5159a9d1cdcf65e892d9f39ca9eeba30e18a611474992f8bcd8aec9d`.
The retained log contains two separate playback runs, not one continuous
five-hour soak:

| Window (local device time) | Player underruns | Steady-state snaps / late drops | Recovered waits |
|---|---:|---|---:|
| 13:59:21–14:47:23 | 0 | unchanged at 1 / 16 after startup | 16 |
| 18:13:37–18:52:36 | 0 | unchanged at 1 / 13 after startup | 20 |

Each run had one initial wait timeout; neither added timeouts during steady
playback. A disconnect at 14:48:28 separates these runs. Current sender state
reports 79 ms configured lead, approximately 81 ms displayed remaining lead,
22 ms maximum capture gap and 1 ms maximum commit time. No capture changes
were made for this milestone. Historical sender settings are not recorded
continuously, so the current reading is not proof of settings throughout the gap.
The kernel pointer-underflow count remains zero with boot still retained.
The latest clock estimate reports 67 µs sync uncertainty and -57 µs sample
alignment error; these are software estimates, not measured acoustic sync.

This is sufficient to preserve the receiver changes as a successful milestone,
not a guarantee against future dropouts. The two living-room Echoes were
connected to the HTPC and were not inspected in this measurement. Their audible
sync is user-reported. Simultaneous voice remains outside this validation.

Evidence: [retained playback counters](sendspin-latency-2026-10-24-milestone.txt).
The race-enabled Sendspin, speaker and output-chain suites passed again
(cached); the candidate's ARM build and targeted vet had already passed.


## Sender capture experiment after the receiver milestone

Receiver milestone commit: `b562fb9`. Firmware remains the same; no new flash.
The sender is outside this Git repository. Its reviewed change is preserved as
[an applicable patch](sendspin-streamer-capture-10ms.patch), relative to
`/home/Mysticle/sendspin-streamer`. Original files were backed up under
`/tmp/echomuse-capture-candidate/` before installation.

- Capture/submission batches: 20 ms → 10 ms, matching the existing 480-frame FLAC.
- Requested Pulse capture latency: 10 ms → 5 ms. `pactl` reports the old capture
  stream's fragment as 1920 bytes / 480 stereo frames (10 ms), and the new one
  as 1024 bytes / 256 stereo frames (5.33 ms). These are negotiated parameters,
  not a physical latency measurement. PipeWire reports zero in its latency
  fields here; that must not be interpreted as actual zero buffering.
- Preserve the ten-second diagnostics window and 500 ms sustained-drift trigger
  when doubling the submission frequency.
- Relabel the displayed number as remaining scheduled playback lead. Replace
  the unmeasured fixed “add 30 ms” claim with the actual requested capture
  configuration and a requirement for loopback measurement.

Validation: Python compilation, tone/click chunk sizes and initialization,
and 100 consecutive 10 ms FLAC submissions for each of mono and stereo emitted
one packet immediately per submission, with exact 10 ms durations and a
contiguous timestamp progression. The desktop-monitor capture is then tested
live with the same connected Radar. The HTPC sender is unchanged.

### 60 ms lead trial

The sender displayed approximately 62–63 ms remaining lead, with ordinary
capture gaps of 11 ms and encode/send maxima of about 1 ms after initialization.
The first minute after the lead change contained one player underrun. Another
occurred at 18:59:39, with the next summary reporting minimum arrival lead
20 ms, median 56 ms, two cumulative player underruns, three snaps and one late
drop. The PCM pointer-underflow count remained zero. This is a failed stability
trial, not evidence that 60 ms is dropout-free. Lead was raised to 65 ms for a
separate trial while keeping the reduced capture buffering.


The 65 ms trial also failed: after initially clean reports, the 19:04:53 summary
recorded two player underruns / wait timeouts, minimum arrival lead 21 ms,
median 60 ms, and zero late drops. Sender snapshots every two seconds covering
that minute showed maximum capture gaps of 11 ms and commit times of 1 ms,
pointing away from sender capture/encoding stalls for those events. The
hardware pointer-underflow count stayed zero. A brief 70 ms trial was clean
but was not run for ten minutes before moving to the next receiver experiment.

### Further receiver experiment: use more of the existing queue as jitter margin

Keep FLAC lossless, the 48 ms target queue and 512-frame pulls. Reduce the
processing/scheduling reserve from 24 to 16 ms and raise the maximum missing-data
wait from 24 to 32 ms. This does not change scheduled playback timestamps; it
spends up to eight more milliseconds of existing queued audio waiting for a
late packet. The tradeoff is less reserve against DSP or scheduler stalls.
The preceding failed trial still had roughly 27 ms queued at a missing-data
timeout, with no hardware pointer underflows, motivating the controlled test.
The successful milestone binary is retained separately before deployment.


The revised receiver is `20261024-1908-dev`, SHA-256
`43352bc3b0611bc4ba272b5e23b692281ca1fc5214129669ca487cc54e9428b7`.
The prior successful firmware is backed up on the device at
`/data/local/bin/server_b.milestone-b562fb9`. The updated reserve passed the
race-enabled Sendspin suite and ARM cross-build before deployment.

At 60 ms lead the revised receiver still had one steady-playback underrun
in the 19:13:52 report, with minimum arrival lead 15 ms and median 60 ms.
The hardware pointer-underflow count remained zero. The trial moved to 65 ms;
its first report at 19:15:53 had zero underruns, zero late drops, one startup
snap, 43 recovered waits and no wait timeouts. This is the baseline for the
subsequent soak, not a continuation of the earlier 65 ms trial on the old build.

During reconnection the sender temporarily displayed “2 in sync” for two
selected discovery aliases of the same Bedroom device. Removing the duplicate
manual selection restored the correct one-device count without restarting
playback. The sender's count is not evidence of acoustic sync between devices.


## Pre-commit verification of `20261024-1908-dev`, 65 ms lead

Firmware `20261024-1908-dev` (fill-wait reserve 16 ms, maximum wait 32 ms), the
sender at 65 ms lead with 10 ms capture batches. Read from the Radar's console
after each manual step; no restarts of the server between 19:09:38 and 19:47.

| Step (device time) | Result |
|---|---|
| Steady playback 19:01–19:28 | 0 player underruns, 0 late drops, 1 startup snap, arrival median 61 ms, minimum 39–43 ms |
| 19:28:52 sender/Wi-Fi stall | One chunk 33 ms past its deadline: +1 underrun, +8 late drops, +1 snap, +1 fill-wait timeout. Recovered unaided; the next minute was clean |
| Wake word mid-stream, 19:32:44 | Voice turn 58 periods, 0 underruns. Music ducked and returned cleanly (user). No new Sendspin underruns |
| Volume buttons, first run, 19:34:40 | The minute added 3 underruns, 3 snaps, 3 timeouts, with arrival never late (minimum 20 ms). Cause not established: the same minute follows the wake test and contains a beam switch |
| Volume buttons, controlled rerun, 19:39:01 | 4 minutes of control then 6 presses: counters unchanged, arrival minimum 42 ms |
| Spotify 19:42–19:45, then back to the stream | No `ALSA buffer thin` lines while Spotify played (ring at its normal depth); the stream resynced at once (`syncErr` 162 µs, 0 underruns) |
| Stop/start twice, 19:46:57 and 19:47:14 | Resynced both times. The first minute after a start carries about 15 late drops and one wait timeout, as in the earlier runs |

Throughout: 0 `mtk_pcm_I2S0dl1_pointer underflow`, 0 server panics, DAC resets 0.
`Interrupt_Handler underflow` lines reached 77 over about 6 hours of uptime,
roughly one a minute, with no relation to any counter above.

Not covered: Music Assistant and grouped playback, simultaneous voice at
sub-80 ms (voice still builds whole 2048-frame periods), a 10-minute soak at
65 ms after the stop/start steps, and any measurement of end-to-end acoustic delay.
Two earlier 60 ms trials and a 65 ms trial on the previous build each dropped out
once or twice a few minutes in; 65 ms on this build had one stall in 46 minutes.

Known noise: the `[speaker] ALSA buffer thin` warning compares against the whole
2048-frame period, so with the 48 ms ring (2304 frames) it fires every 10–40 s
during healthy low-latency playback. It should be silenced for low-latency
streams in a follow-up; not changed here because that code is not in the
tested binary.


## Where the remaining arrival jitter comes from (evening, `20261024-2011-dev`)

The arrival minimum at 65 ms lead swings between about 25 and 45 ms against a
61 ms median, and each underrun since the fill wait has been an arrival
outlier rather than anything on the write side. Pinging the Echo from the
streamer PC at 100 Hz (wired PC, Echo on 5 GHz at -43 to -50 dBm) split it
into three parts:

- **The network path is clean most of the time**: p50 1.5 ms, no loss.
- **CPU hotplug on the Echo.** HPS parked and restarted CPU3 every 1-2 s at
  this load (145 `CPU3: shutdown` lines in the kernel ring), and a hotplug
  stops every CPU while it runs. Two-minute A/B with the floor at 2 and then 4
  (`/proc/hps/num_base_perf_serv`): replies over 20 ms 129 → 44, over 10 ms
  198 → 101, p99 20.9 → 7.6 ms. With the floor at 4 the longest fill wait in
  the following minutes fell from the whole 32 ms budget to 21 ms. The
  firmware now holds every core online while a low-latency Sendspin stream
  plays (`cmd/sendspin.go`, `sendspinCores`), dropping back to the usual 2
  thirty seconds after the stream ends.
- **The access point.** The remaining 10-60 ms stalls land on a 5 s grid at
  the same phase (…4.93, …9.93) and hit every wireless client at once: the
  Echo and two other Amazon devices stalled in the same 10 ms, while the wired
  gateway and the AdGuard host, pinged from the same PC in the same minute,
  did not. The AP is a Netgear RAX70 on SWRT (Broadcom); the Echo is on
  `eth6`, channel 52/80 (DFS), the radio `acsd2` manages, with Smart Connect
  (`bsd`), `roamast` and `conn_diag` running. `acsd2`'s in-service scan timer
  reads 20 s at runtime, so it does not match the 5 s grid on its own. Which
  daemon it is was not established: pausing them one at a time is the next
  test, and it changes the user's router, so it is theirs to run. A 50 ms
  stall at 20:10:04 (arrival minimum 11 ms) caused one underrun at 65 ms lead.

Not the cause, each checked: Wi-Fi power save (forced off by the driver),
Nagle (aiohttp sets `TCP_NODELAY`; aiosendspin's writer sends a chunk as soon
as it is queued), the PC's Wi-Fi interface answering for the wired address
(the Echo's ARP entry is the wired MAC), multicast load on the BSS (67 packets
in 10 s), DNS (nothing on the stream path resolves a name).

**Streamer: a cutout that was not the Echo.** At 20:03 the Echo's mDNS record
lapsed, the streamer deleted the player, the group lost its only member and
the stream stopped ("music source starved" on the Echo, a new stream when the
user re-selected the manual entry). The streamer now keeps a player whose
connection is live through a lapsed record, and counts an Echo selected under
two names once. [Patch](sendspin-streamer-keep-lapsed-players.patch).

**Log noise.** `ALSA buffer thin` is judged against half the shallow ring
(24 ms) while the speaker builds short periods, instead of a whole 2048-frame
period that sits above the shallow ring's ordinary depth.

**Which router daemon.** The user paused `bsd`, `roamast`, `conn_diag` and
`acsd2` for 40 s each while the Echo was pinged at 50 Hz. Stalls on the 5 s
grid: baseline 3, `bsd` 1, `roamast` 2, `conn_diag` 3, `acsd2` **0**, and
they came back 3 s after `acsd2` resumed. The user then fixed 5 GHz-1 at
44/80 (non-DFS; the one neighbouring network in that block has its control
channel on 44), which leaves `acsd2` nothing to manage and it exits. Over
three minutes of ping: stall events 8.5 → 5.0 a minute, on the 5 s grid
3.3 → 1.3, replies over 20 ms 13.4 → 7.0, worst 47.7 → 40.6 ms. A smaller
5 s source remains and was not identified.

| Run, `20261024-2011-dev`, channel 44 | Steady-play underruns | Arrival minimum per minute |
|---|---:|---|
| 65 ms, 20:35-20:44 | 0 | 33-43 ms |
| 60 ms, 20:46-20:55 | 2 (one at 20:51, starved again 20:56) | 12-36 ms |

65 ms stays the setting. 60 ms is still short of what this link's tail
needs, by roughly the 20-30 ms outliers that remain after the channel change.

## The 20-minute soaks, and delayed ACKs

With every change above in place (channel 44, Smart Connect off, the
streamer marking audio CS6 for WMM voice, core floor 4), 20 minutes at 65 ms
was **not** clean: three underruns between 21:15 and 21:26 (21:18:34, two at
21:24:45), each beside a 55-60 ms ping reply. 70 ms then ran 12 minutes clean
through volume and mute changes over the streamer API, and had three more
underruns in the 8 minutes after a stream stop/start.

The arrival median does not sit at a fixed distance below the lead: it was
55 then 61 ms at a 65 ms lead, and 68 then 60 ms at 70, changing only across
a stream restart, while the streamer's own figure (`latencyMs`, play time
minus now at commit) stayed at 72-74 ms and the clock filter's offset moved
smoothly. Five restarts in a row at 70 ms all landed at 60. Where the other
8 ms goes on a bad start is not found.

The socket explained the stall size. `ss -tni` on the PC showed **12.3 ms
smoothed RTT** on the audio connection while ping read 1-2 ms: the Echo ACKs
every second segment, and a live stream is one ~600-byte segment every 10 ms,
so each ACK left one chunk late. The sender's loss recovery is paced by that
RTT (RACK's reorder window, the tail-loss probe at 2xRTT), so one segment
lost in the air cost ~25-35 ms before it was resent. The Sendspin session now
re-arms `TCP_QUICKACK` after every read (`internal/sendspin/quickack.go`),
and the same socket reads **1.8 ms**.

| 20 min at 70 ms, channel 44 | Underruns | Arrival minimum per minute | Ping replies 47-60 ms |
|---|---:|---|---:|
| `20261024-2011-dev`, 21:36-21:56 | 3, all after the restart | 21-54 ms | 5 |
| `20261024-2212-dev` (quick ACK), 22:12-22:33, two restarts | **0** | 29-56 ms; 38-47 ms in the 60 ms-median phase | 5 |

The second run had the same stalls on the air and the same bad-start median
(60 ms), and nothing reached the speaker. 70 ms is the setting.

## 40 minutes of rough handling at 70 and 80 ms

Each run: 5 minutes plain play, then volume sweeps every 15 s with mute
toggles, two busy loops on the Echo's CPU, four stop/starts a minute apart,
two deselect/reselects, CPU load with volume changes and a restart together,
and 5 calm minutes.

| `20261024-2212-dev` and later, channel 44 | Steady-play underruns | Real XRUNs |
|---|---:|---:|
| 70 ms, idle between soaks, 22:33-23:08 | 3 (22:42, two at 22:57), each in the 60 ms-median state | 0 |
| 70 ms, 40 min (with a bulk-download phase) | 1 at a 1.9 s whole-device freeze (23:10:17); 9 during the download; 0 otherwise | 0 |
| 80 ms, 40 min (download phase replaced with plain play) | 1, at 00:47:51: a chunk 101 ms past its play time, beside a 104 ms ping | 0 |

The download phase pulled the firmware binary from the PC in a loop for 4.5
minutes. Nothing in normal use downloads to an Echo that hard, so it was left
out at 80 ms. The one 80 ms underrun needed a lead of ~180 ms to absorb, so a
longer lead would not have helped. Volume, mute, CPU load, restarts and
reselects caused no underruns at either lead.

**Where the lost margin goes depends on how a stream starts.** After a
streamer stop/start the median arrival lead sat ~10 ms under the lead (60 at
70 ms, 70 at 80); after a deselect/reselect it sat at the lead (69-70 at 70,
76-78 at 80). Ruled out: the clock filter (offset smooth throughout), the
server's buffer backpressure (the Echo declares 576 KB; the duration cap is
30 s), the Echo's decoder (decodes each frame as it arrives), the server's
stereo-to-mono graph (480 samples in, 480 out, no hold) and the streamer's
own timeline (`latencyMs` 72-74 at 70 across restarts). Finding it is worth
~10 ms.

**What 268bbbc's wider fill wait buys.** The fill wait now also counts
`pastTight`: periods that arrived after b562fb9's 24/24 ms budget would have
given up and inside the current 32/16. Over the 40 minutes at 80 ms it stayed
at **0** across ~130 waits, so at 80 ms 268bbbc behaves like b562fb9. Its
smaller reserve cost nothing either: no real XRUN in about four hours, two
CPU-load phases included. The difference can only show at leads where packets
run that late, 70 ms and under.
