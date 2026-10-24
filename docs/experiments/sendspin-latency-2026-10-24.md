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
