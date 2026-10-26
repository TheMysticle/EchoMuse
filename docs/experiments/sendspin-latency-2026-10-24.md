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

## 2026-10-25: the streamer keeps its own timeline, against b562fb9 at 79 ms

**Why stop/start lost ~10 ms.** aiosendspin 9.1.1 only ever moves a stream's
timeline later, and two of its stages ignore the moves. The resampler that
makes the Echo's mono stamps output from its own clock and ignores input jumps
up to 20 ms; `_encode_for_transform_key` stamps frames from the encoder's
`pending_timestamp_us` whenever that is within 500 ms of the commit. So frames
left the PC earlier than the commit timeline by 0-20 ms, a different amount
each start, and the streamer's `latencyMs` (the commit timeline) was not what
the Echo received: a first start showed "100 ms" with frames carrying 78. The
patch (`sendspin-streamer-resampler-follows-timeline.patch`) makes both stages
follow the timeline, and gives the streamer its own (`OwnTimeline`): chunk n
plays at anchor + n x 10 ms; the first 500 ms go out as silence while the
anchor is measured; after that it is only steered, at most 2 ms per 2 s (under
the Echo's 5 ms snap), except that a lead more than 10 ms short for 400 ms
moves later at once, as the library's own rebase would. Offline simulation:
Pulse's +12 ms capture creep after a start is absorbed in seven steps of at
most 1.6 ms; a lasting +30 ms capture jump costs one 30 ms gap.

Four stop/start and reselect cycles, Echo arrival median (raw:
`sendspin-latency-2026-10-25-owntimeline.txt`):

| | cycle 1 | 2 | 3 (reselect) | 4 |
|---|---:|---:|---:|---:|
| before (resampler fix only) | 76 | 76 | 76 | 70 |
| OwnTimeline | 79 | 79 | 79 | 79 |

Every start settled within 2 ms of its target; no emergency move.

**20-minute soak at 80 ms**, stop/start at minute 7, reselect at 14: 0 real
XRUNs, 18 clean minutes, and 2 underruns 12 and 22 s after the stop/start. The
streamer's log shows every chunk in that window leaving the PC with at least
72.9 ms of lead; the Echo received one 111 ms past its play time, ~190 ms in
transit. Network or Echo side, the same class as the 00:47 stall at 80 ms.

### Against what was running on 2026-10-24 at 16:39

The user watched YouTube on the two living-room Echoes (fed by the HTPC) at
16:39 with no audible fault. Those Echoes run `20261024-1357-dev`, which is
b562fb9's source (built 13:57, committed 18:54 with no edits between). No log
from them or from the HTPC exists; the same build's runs on Bedroom from this
PC are in `sendspin-latency-2026-10-24-milestone.txt`.

| | 2026-10-24 (1357 = b562fb9) | 2026-10-25 (124c763 + streamer patches) |
|---|---|---|
| Fill wait (budget/reserve) | 24 / 24 ms | 32 / 16 ms |
| TCP quick ACK, core floor 4 | no, no (floor 2) | yes, yes |
| Lead | 79 ms (≈81 shown, library timeline) | 80 ms (own timeline) |
| Capture batches / Pulse latency | 20 ms / 10 ms | 10 ms / 5 ms |
| DSCP | none | CS6 (WMM voice) |
| Router 5 GHz | auto channel, acsd2 stalls | channel 44/80, acsd2 gone |
| Bedroom playback | 48 min (13:59-14:47) + 39 min (18:13-18:52), no restarts | 20 min soak + 4 cycles, 6 restarts; 40 min rough soak the night before |
| Steady underruns | **0 in 87 min** | **2 in ~30 min** (both just after a restart); 1 in 40 min |
| Fill-wait timeouts after start | 0 | 1 burst |
| Real XRUNs | 0 | 0 |

The afternoon runs had no restarts in them and a worse router, and were still
clean, so the network is not an obvious explanation by itself. Two streamer
changes since then alter what goes on the air and have no measurement of their
own: twice the packet rate (10 ms batches) and CS6 marking, which puts audio
in the WMM voice queue, where many drivers send without aggregation. Either
could lengthen a stall. Next: A/B on Bedroom with today's firmware and the
streamer returned to 20 ms batches without DSCP, OwnTimeline kept.

### A/B on Bedroom, 2026-10-25 (times +01:00, after the DST change)

Same firmware (124c763) and OwnTimeline throughout; only the streamer's
batch size, capture request and marking changed.

| Run | Batches / capture / DSCP | Length | Underruns | Worst arrival min | Real XRUNs |
|---|---|---|---:|---:|---:|
| soak 1 (02:31) | 10 ms / 5 ms / CS6 | 20 min, stop/start + reselect | 2 | — | 0 |
| A/B (02:04) | 20 ms / 10 ms / off | 20 min, stop/start + reselect | 0 | 39 ms | 0 |
| soak 2 (02:27) | 10 ms / 5 ms / CS6 | 20 min, stop/start + reselect | 0 | 33 ms | 0 |
| untouched (02:47-04:17) | 10 ms / 5 ms / CS6 | 90 min, no interaction | **0** | 20 ms | 0 |

The 90-minute run matches the 87 clean minutes of 2026-10-24 at 10-15 ms less
real delay: underruns 0, lateDrops 0, snaps 2 (both at stream start), arrival
median 78-80 ms in every minute, one "ALSA buffer thin" note (23.3 ms queued,
03:23:36) and no XRUN. Ping alongside: 27,193 sent, 0 lost, max 61 ms, nothing
above 33 ms in the minute of the 20 ms arrival minimum. So today's settings do
not stall on untouched playback; the two underruns of soak 1 followed a
stop/start, and one in three 20-minute runs with interaction is all the
evidence against them. 20 ms batches stay rejected for their extra ~10 ms.
Next: long runs WITH interaction on today's settings, then DSCP off at 10 ms
batches if those show stalls.

### DSCP with interaction, automated (2026-10-25 11:14-13:14 +01:00)

The streamer gained `POST /api/config` (`chunkMs`, `captureMs`, `dscp`, all
while running; DSCP is re-marked on open sockets with no break), so both runs
were driven from one script with no restart between them. 10 ms batches and
5 ms capture in both, 60 min each, a stop/start or a reselect every 10 min.

| Run | DSCP | Underruns | Snaps after start | Worst arrival min | Ping >20 ms / max |
|---|---|---:|---:|---:|---|
| A 11:14 | off | **0** | 0 | 40 ms | 65 / 40 ms |
| B 12:14 | CS6 | **1** (12:48:33) | 1 | 11 ms | 163 / 50 ms |

B's underrun came mid-stream, 9.5 min after a stop/start, not after an
action: one packet reached the Echo ~70 ms late (arrival min 11 ms), with a
fill-wait timeout, a snap and a 15th late drop. The streamer sent every frame
in that window with at least 78.2 ms of lead, and ping (best-effort queue)
saw nothing above 9.3 ms within 20 s of it. The "music source starved" lines
right after each reselect, in both runs, are the player being removed by the
reselect itself.

Across the day, CS6 has 3 underruns in 190 min (2 in the first 20-min soak,
1 here) and no DSCP at 10 ms batches has 0 in 60 min; yesterday's 87 clean
minutes were also unmarked. That is short of proof, but DSCP buys nothing
measurable and costs no delay to drop, so the streamer's default is now
10 ms / 5 ms / DSCP off.

## The remaining stalls come from the router's radio, on a 5 s grid (2026-10-25 15:25-15:50 +01:00)

Read during the first 75 ms run, with the Echo untouched apart from
read-only queries.

**Nothing is lost; packets arrive late.** The PC's audio socket showed
`retrans:0/31 dsack_dups:31` after 11 minutes: every retransmission the
sender made was reported back by the Echo as a duplicate, so the original had
arrived, only after the sender's ~5 ms probe timer. About three a minute.

**The late ones keep time.** Logging that socket's retransmit counter once a
second, 15 of 20 seconds with a new retransmit fell in the same two seconds of
a 5 s cycle; 19 of 24 Echo ping replies over 10 ms (5 Hz) fell in one
half-second of it, at 3.5 s past each multiple of 5 s of Unix time.

**Every Wi-Fi client sees the same stall at the same moment.** Pinging three
other wireless clients (.116, .82, .4; 1.6-1.9 ms median) at 5 Hz for 10
minutes alongside the Echo: each had 52-61 replies over 10 ms peaking in the
same half-second, and 35 of the Echo's 50 spikes were matched within 250 ms on
all three. There was also one 4-second burst, 15:38:46-50, of 11-82 ms
replies on all four at once.

**The router's CPU and its wired side do not stall.** Five minutes of 5 Hz
ping to the router itself (answered by its CPU): median 0.42 ms, p99 0.6 ms,
max 8 ms, slow for 1 of the 23 Wi-Fi spikes in that window. A wired client
(.211): max 0.3 ms. So the router's radio holds downlink to every station for
10-30 ms on a 5 s timer while its CPU is idle. That is the "smaller 5 s
source" left after acsd2 was stopped on 2026-10-24, and nothing on the Echo
can remove it.

Ruled out on the Echo, all read-only:

| Suspect | Finding |
|---|---|
| Group-key rekey | The router rekeys every 3600.03 s (hh:13:20, EAPOL `R1X len=145`/`T1X len=113` in dmesg). The minutes holding one read 62 and 63 ms arrival minimum at 80 ms, which is normal. |
| Background scans | No `bgscan` in the conf, `disable_scan_offload=1`, no scan or roam lines from the driver in 15.5 h of kernel log. |
| Power save | WEXT `SIOCGIWPOWER` reports power management disabled; `iwpriv get_power_mode` is unsupported. Moot anyway: other stations stall at the same instant. |
| Aggregation | Two `+RxBA` setups in 15.5 h (TID 2 and 7), none near a stall. |
| BLE coexistence | `bleProxyEnabled=false` on Bedroom. |
| The serial console | The USB ACM gadget logs every console write to the kernel ring, but `printk.disable_uart=Y` (one UART interrupt since boot), so reading the Echo costs no UART time. It does shorten dmesg's history to ~15 h. |
| init's 5 s supervision loop | Reads `carrier` and the address; no supplicant or driver call. |

**What drives it is not yet known; the router's settings rule some things
out.** On the router (RAX70 on SWRT, `RT-AX95Q` profile, kernel 4.1.52), the
Echo and the three clients that stalled with it all sit on `eth6` (5 GHz-1,
44/80). `acsd2` is not running, band steering is off (`smart_connect_x=0`, no
`bsd` process), radio power save is off, DTIM 1, beacon 100 TU. Still running
and able to touch the radio periodically: `roamast` (`wl1_user_rssi=-70`),
`conn_diag`, `networkmap`, `wlceventd`, the `amas_*` mesh daemons, plus the
driver's own MU-MIMO/TxBF sounding (`wl1_mumimo=1`, `wl1_txbf=1`) and periodic
calibration (`wl1_cal_period=-1`, driver default). Telling these apart needs
the router's process wakeups on the 5 s grid, which waits until no soak is
running: a first try at sampling every process's `/proc/<pid>/status` from the
router's busybox shell kept a core busy for 90 s (16:00-16:01:28), and that
minute of the 75 ms soak read a 33 ms arrival minimum against 49-56 ms
elsewhere. No underrun, but minutes 36-37 of that run are marked as perturbed.

### 75 and 70 ms, automated with interaction (2026-10-25 15:24-17:24 +01:00)

Firmware 124c763 (`20261025-0030-dev`), streamer 10 ms batches / 5 ms
capture / DSCP off, OwnTimeline. 60 min at each lead, a stop/start or a
reselect every 10 min (six per run), Echo read each minute, 5 Hz ping and the
audio socket's `ss` counters alongside.

| Lead | Underruns | Real XRUNs | Worst arrival min | Fill waits per 10 min (pastTight) | Ping max / >20 ms |
|---|---:|---:|---:|---|---|
| 75 ms | **0** | 0 | 33 ms (the perturbed minute; 35 otherwise) | 6-30 (0) | 37 ms / 106 |
| 70 ms | **0** | 0 | 26 ms | 70-112 (up to 3) | 49 ms / 89 |

Both: one fill-wait timeout per stream, at its start, as at 80 ms; the
"starved" and "ALSA buffer thin" lines fall at each reselect, from the player
being removed. One mid-stream "ALSA buffer thin" at 70 ms (17:12:18), with no
starvation and no underrun. The 70 ms run spent its margin: fill waits were
3-10 times as frequent as at 75, and pastTight reached 3, so three periods
were saved only by 268bbbc's wider budget. Neither setting has met the pass
bar yet (2 hours with interaction, then a sync check on two Echoes); 75 ms is
the next candidate for it, and no rare 70 ms network stall like 12:48:33's
fell inside these two hours.

### What the router does on the 5 s grid (2026-10-25 17:40-17:55, no soak running)

Router threads' `voluntary_ctxt_switches` sampled at ~3 Hz for 150 s (one
`grep` per sample; the shell loop of 16:00 was the costly part), with 5 Hz
ping to the Echo and .116. Two threads keep a 5 s period at the stall phase:
`fc_evt`, the Broadcom flow-cache event thread (708 wakeups, 65% in one
half-second; all 5 ping spikes of the window within 0.03 s of one of its
bursts), and `miniupnpd` (584, 59%), which wakes half a second earlier.
`roamast` runs on a 10 s period at another phase and is cleared. Neither
`miniupnpd` (`notify_interval=60`, `clean_ruleset_interval=600`) nor the flow
cache (`Flow Timer Interval = 10000 millisecs`) has a 5 s timer of its own.

**Something on the LAN does.** Listening to multicast from the PC for 90 s:
192.168.1.50 (`f4:b1:c2:xx:xx:xx`, wired: on none of the router's radios, no
DHCP lease) sends one 101-byte SSDP `M-SEARCH` every 5.00 s (4.78-5.22),
always at phase 2.5 s, just before `miniupnpd` wakes. Every UPnP device
answers each search, and the router's wireless multicast forwarding
(`wl1_wmf_bss_enable=1`) sends the search itself to each Wi-Fi station as
unicast.

Three minutes of its search times against 10 Hz pings to three Wi-Fi
clients: of 11-12 replies over 10 ms per client, 4-5 came 0.5-0.7 s after a
search, a window holding 20% of the time. A second group sat 2.7-3.2 s after
one, which .50 does not explain. So .50's search is one trigger of the
stalls, not shown to be the only one. The test that settles it is to stop
that device's searching for ten minutes and count the stalls; that is a
device on the user's network, not something to switch off from here.

**The door station.** .50 is the building's Dahua VTO door station. Its UPnP
page has UPnP disabled (every port mapping reads "Failed"), and it searches
anyway: `M-SEARCH`, `ST: upnp:rootdevice`, `MX: 5`, every 5 s, from built-in
discovery that page does not control.

**Filtering it from the radios made things worse (18:13-18:23).** The user
added, on the router's bridge, `ebtables -I FORWARD -s f4:b1:c2:xx:xx:xx -o
eth5|eth6|eth7 -p IPv4 --ip-proto udp --ip-dport 1900 -j DROP`. The counters
showed one drop per radio per search. 10 min of 10 Hz ping to three Wi-Fi
clients, against the 3-minute baseline at 17:5x:

| | >10 ms per min | >20 ms per min | Max |
|---|---:|---:|---:|
| Baseline | 3.7-4.0 | 2.3-2.7 | 38 ms |
| Filter on | 14.3-14.9 | 8.3-10.4 | 62 ms |

The spikes still came 0.5-0.7 s and 2.7-3.2 s after each search, although
Wi-Fi no longer received it. So the copies sent to the stations are not the
trigger. **The fourfold rise was not the rule**: the 2.4 minutes after it was
removed (18:29-18:31:30, no rule of any kind) read 12.8-16.5 per minute over
10 ms and 7.4-9.9 over 20 ms, the same as with it. The network was simply
busier in the evening than at 17:5x, and the first reading here blamed the
rule for that. The Echo (75 ms lead) had no underrun or XRUN through it; worst minute
32 ms. The rule was asked to be removed straight after.

**An iptables INPUT drop of its searches (from 18:31:30) changes nothing
measurable.** The user added `iptables -I INPUT -s 192.168.1.50 -p udp --dport
1900 -j DROP`, one drop per search; 7.6 minutes with it: 10.6-12.4 per
minute over 10 ms, 4.9-7.4 over 20 ms, against 12.8-16.5 and 7.4-9.9 in the
2.4 minutes before it. 15-30% fewer, from a baseline too short to call it; left in place for the passive 70 ms run that started at 18:31.
The door station's searches continued after the user also unticked every
service on its UPnP page.

### 70 ms for two hours, no actions (18:31-20:31): two underruns, fails

Streamer at lead 70, 10 ms batches, 5 ms capture, DSCP off; one stream for
the whole run, no stop/start or reselect; the iptables drop above in place
throughout. The user pressed the Echo's volume-down buttons at about 18:38
and watched a film on it during the run.

| | |
|---|---|
| Underruns | **2**: in minute 10 (18:40-18:41, arrival minimum -13 ms, 6 late drops, a snap, a fill-wait timeout) and minute 17 (18:47-18:48, arrival minimum 14 ms, a snap, a fill-wait timeout) |
| XRUNs (`mtk_pcm_I2S0dl1_pointer underflow`) | 0 |
| Worst arrival minimum otherwise | 27 ms (minute 3); 31 ms or more in every later minute |
| Fill waits | 449 in 120 min, 2 pastTight, 2 timeouts (the two underruns) |
| Ping to the Echo, 0.2 s | 35,862 samples, none lost, max 60.4 ms at 18:47:58 |

The second underrun lines up with the run's one ping stall over 60 ms. The
first does not: the worst ping in that minute was 34 ms, so a packet 83 ms
late on the audio socket came with no comparable stall on ICMP. The Echo's
log for those minutes had already been trimmed when it was read. Both fell
in the first 17 minutes; the remaining 103 were clean, which is the rare
40-70 ms stall arriving twice in close succession, as before, and not a
property of a stream's first minutes (this stream started at 18:31, nine
minutes earlier). `Auddrv_DL1_Interrupt_Handler underflow` lines ran at 4-9
an hour through the run, as at 80 ms; they are not underruns.

70 ms fails the pass bar. 75 ms passed one hour with interaction and has no
two-hour run; 80 ms stays the setting.

The same stream ran on unattended at 70 ms until it was read at 23:01, 4 h
30 min in all. One more underrun, at 20:59-21:00 (arrival minimum 18 ms, a
snap, a fill-wait timeout, "ALSA buffer thin" at 20.0 ms 30 s before), so 3 in
4.5 hours, each with a fill-wait timeout; none in the two hours after it.
Arrival minimum under 30 ms in five other minutes (24-28 ms) without one.

### Fill-wait reserve 16 -> 8 ms (build 20261025-2308-dev, from 23:10)

Shrinking the 48 ms ring would add no tolerance: `fillWaitDeadline` runs to
the period's play time less the reserve, whatever is queued, so a packet has
to arrive by then however deep the ring is. The margin is the reserve. This
build takes it from 16 to 8 ms and the longest wait from 32 to 40 ms; the old
16/32 budget becomes the "tight" one, so `pastTight` now counts the periods
the 8 ms reserve saved. The cost would be a real XRUN when the write after a
long wait comes later than 8 ms; that is what this run has to show. Only low-latency
streams wait at all (`waitForPeriod` returns at once unless `shallow`).
Previous firmware kept on the Echo as `server_b.0030-reserve16-80ms`.
Passive 70 ms run from 23:10:52, two hours; the 16 ms run's files are in the
scratchpad's `run-reserve16/`.

Result, 23:10:52-01:10:54, 70 ms lead, interaction in the first 20 minutes:

| | |
|---|---|
| Underruns | **1**, at 23:29:25: a Wi-Fi freeze of ~650 ms (three pings answered together at 545/337/128 ms), arrival minimum -622 ms, 68 late drops, a snap. No buffer under ~700 ms survives it, and no user action came near it |
| XRUNs (`mtk_pcm_I2S0dl1_pointer underflow`) | 0, through all of the interaction |
| Saved by the 8 ms reserve (`pastTight`) | 2: minute 58 (00:08, arrival minimum 18 ms, the depth that underran at 21:00 on the 16 ms reserve) and minute 64 (00:14, 24 ms, ping 47 ms at 00:13:56) |
| Fill waits | 1146, 1143 recovered, 3 timeouts (one at stream start, two in the freeze) |
| Arrival minimum, other minutes | 18-34 ms in eleven of them, otherwise 47 or more |

Interaction (user): volume presses at 23:12 and many at 23:15; wake word and a
question at 23:13, 23:14 and 23:17 (loud music, score 0.984); a 10 s timer at
23:30 stopped after three rings; a false wake at 23:43 (score 0.295 on music,
cancelled with the button) after which the mic was muted. Every voice turn
played with 0 voice underruns and changed no music counter except fill waits,
all recovered inside the old budget. The two living-room Echos were powered
on at 23:51; no stall followed.

Against the 16 ms reserve over 18:31-23:01 (3 underruns in 4.5 h, two on
packets 14 and 18 ms early), the 8 ms reserve took both depths that had cost
an underrun without one and without an XRUN. Its only underrun was a freeze no
setting covers. One two-hour run is not the pass bar: it needs repeating, and
the freeze needs explaining.

### The streamer stopped redialling after an Echo went away (2026-10-26)

Blocking the PC on the Echo for 40-50 s (iptables INPUT drop on the Echo)
reproduced "the streamer only comes back after a restart". The Echo was
fine: it dropped the dead socket at its 30 s read deadline and was free
again. aiosendspin 9.1.1 then logged "Role queue full for player
(4096/4096), client too slow - disconnecting" and never dialled again.
`disconnect()` cancels the message loop, a cancelled loop does not set
`_connection_done`, so `handle_client` waits on it forever; the dial task
stays alive and every `connect_to_client()` only pokes its retry event.
The streamer now sets the event after `disconnect()` (patch file). Same
test after the fix: overflow at 01:40:32, unblocked 01:40:45, connected and
in sync at 01:40:54 with no restart.
