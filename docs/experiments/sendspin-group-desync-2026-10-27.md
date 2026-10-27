# Living Room / Lounge desync after a voice reply (2026-10-27)

Setup: TV optical → ICUSBAUDIO7D on the Unraid NAS → Sendspin streamer
(container, 50 ms lead) → Living Room (G2A0…05HU, right) and Lounge
(G090…10Q0R, left), Radar boards on emOS v0.10-gtk.1, firmware
20261026-0207-dev.

## Symptom

After speaking to Living Room the pair is audibly out of sync. Every
self-reported figure says otherwise: the player's `lastErr` stays around
0.1 ms, and the streamer reports "2 in sync".

## Measurement

`device/tools/dac_probe` records the DL1_AWB_Record tap (pcm9 capture), which
is the speaker's output stream inside the SoC, beside the running firmware. It
stamps each 2048-frame read on the monotonic clock and maps that onto the
host clock through 200 UDP exchanges before and after the capture. Both Echos
are captured at once, and `align.py` (GCC-PHAT, ±200 ms) prints how far
Living Room plays after Lounge.

Both players were set to "stereo" for the measurement. With left and right
content the cross-correlation locked onto false lags (−10, −25, +19 ms).

| Condition | Living Room after Lounge |
|---|---|
| Fresh firmware on both, before any reply | +0.05 to +0.22 ms |
| Lounge static delay +20 ms (method check) | +20.10 ms change |
| After HA `assist_satellite.announce` "Testing." on Living Room | **+42.7 ms, steady** |

42.7 ms is exactly 2048 frames, one loop period.

## Mechanism

Living Room's log at the announcement:
- `ALSA buffer thin — 2000 frames queued (41.7ms)`
- `music source starved`
- the substream's `trigger_time` changed twice

The kernel log shows two `mtk_pcm_I2S0dl1_prepare` and `start` pairs 0.44 s
apart, with `aic32x4_apply_mute`. Those are XRUN recoveries: tinyalsa
re-prepares inside `pcm_write`.

The cause of the underrun:
- A low-latency stream holds the ring at 48 ms and pulls music in 512-frame
  slices.
- A voice period is a whole 2048 frames, and the music under it was pulled at
  that size too.
- At a 50 ms lead, the end of a 2048-frame period due 48 ms out has not
  arrived yet. The fill wait therefore spends the ring down to its 8 ms
  reserve.
- The 2048-frame build (response mix, Radar FIR and MBCL) plus the write then
  runs the ring dry.

After the restart the DMA plays one period later than ALSA's `delay` and
`hw_ptr` say. The firmware places audio by that `delay`, so it cannot see the
error. Only a clean reopen (a firmware restart) clears it.

Ruled out:
- The outchain FIR: overlap-save is exact.
- GoTinyAlsa buffering: `Pump` writes directly.
- The Sendspin time filter: offsets drift smoothly at 4–6 ppm.

## Fix (verified on the pair)

`silenceLoop` now plays a voice period in the same 512-frame slices as music
whenever a low-latency source holds the pull plane (`voiceSlice`,
`ringwait.go`). Each slice is throttled to the shallow ring, and the music
under it is asked for 512 frames, so the fill wait no longer has to drain the
ring. Music under the reply is kept.

Build 20261027-1859-dev (sha256 `c31c4b1a…bcda0`), flashed on both Echos and
measured the same way, with both players on stereo:

| Condition | Living Room after Lounge |
|---|---|
| Before any reply | +0.14, +0.12 ms |
| After announce "Testing." | +0.08, −16.55, +0.05 ms |
| After a ~6 s announce | +0.12, +0.18, +0.20 ms |

The −16.55 ms reading is a single outlier between two readings at ~0 ms, most
likely a false correlation peak.

Across both replies there was no `I2S0dl1_prepare` after the firmware's own
start, and no `ALSA buffer thin`, `starved` or `XRUN` lines in the log. The
old build had two prepares per reply.

Not fixed: if a PCM restarts for any other reason (a Wi-Fi freeze longer than
the ring), the one-period offset still sticks until the next restart. The
candidate fix is to detect a `trigger_time` change in the status file
`pullSource` already reads and reopen the PCM cleanly.

## Second issue: a restarted player did not rejoin the TV stream

When Lounge's firmware restarted while it led the stream, aiosendspin's late
join failed, and it stayed silent until the stream was restarted by hand. The
streamer now restarts the stream whenever a player joins a running one
(`sendspin-streamer-rejoin-restart.patch`). Verified: after a Lounge firmware
restart, both were playing again within ~11 s.

## Unrelated log fix

`[group] stop` was logged 10 times a second while another server held the
music slot (595 lines a minute), because `spotifyPoll` discards the ring each
tick. It is now logged only when there was audio to cut (`leader.go`).
