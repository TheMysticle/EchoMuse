# Streamer auto stream, and the lead running away with the TV off (2026-10-28)

`sendspin-streamer-auto-stream.patch` applies on top of the TV-mode patch.

## Auto stream

`auto_stop_min` (default 10; 0 = never). After that long with every chunk's
peak at or under −60 dBFS, or with no chunks at all, the streamer releases
the players exactly as Stop does:
- the stream ends;
- the group is stopped;
- the ticked players are re-dialled with a discovery reason (`ticked`
  includes `not self.auto.idle`). Holding a playback dial with nothing playing
  is the 5 s swap with the speaker group recorded at `_dial`.

The capture keeps running. The first chunk over −60 dBFS re-dials for
playback, and the stream loop starts the stream once the leader is ready.

The rules are in `AutoStop` with an injected clock (`test_auto_stop.py`, 9
tests). Its only hot-path cost is one comparison on the peak that `_meter`
already computes, so nothing is added to the audio path.

Live, with auto stream at 15 s:

- **Going idle:** after 15 s of a silent TV the players were released. Lounge
  returned to its speaker group, which Living Room leads, and refused
  discovery dials ("is up but not accepting", one retry every 10 s). Living
  Room stayed connected. The state was steady and the stream did not churn.
- **Waking:** sound returned at 18:32:00.50.
  - Living Room's stream had settled by 18:32:01.09 (0.6 s).
  - Lounge's join restarted the stream, which had settled again by
    18:32:01.74 (1.2 s).
  - The first 1–2 s of sound are lost: the playback re-dial, then the
    timeline's 500 ms warm-up of silence.
  - The pair after waking (dac_probe on TV audio): −0.14 and +0.08 ms.

## The lead ran away while the TV was off

With no S/PDIF signal the ICUSBAUDIO7D free-runs on its own clock.
`/proc/asound/ICUSBAUDIO7D/pcm0c/sub0/status` read `hw_ptr` going up by
50,165 and then 50,015 frames a second, against 48,000. That is 2,074 frames
per 10 s window where 2,000 is normal. `OwnTimeline` steers by at most 2 ms
every 2 s, so the queued lead climbed from 50 ms to 615 ms in 25 s. When the
TV came back the card locked to 48 kHz and kept the surplus, so the sound
played seconds behind the picture. This matches the user's report that the
large latency appeared only after the TV had been off.

Fix: `OwnTimeline.next(now, quiet)` drops a chunk when it is silent (peak
≤ −60 dBFS) and the lead is more than one chunk (10 ms) over the target.
- A correct-rate input never gets that far over. Steady state is the target
  ±2 ms, and a late chunk only lowers the lead.
- So it never acts on programme, and a chunk with sound is never dropped.

Live, with the TV off and auto stream at 0, the lead held at a median of
54.4 ms (target 52) over 40 s.

`test_timeline.py` (5 tests):
- a 50 kHz silent input holds the lead;
- a correct-rate silent input with 0.7 ms jitter drops nothing;
- sound running 4 % fast is never dropped;
- two minutes off, then programme, starts at the lead;
- 300 ms pauses in speech are not shortened.

Still seen with no signal: the resampler logs a timeline jump of about 7–8 ms
roughly every second. This was there before the fix, stopped once the TV was
on, and the patched resampler follows each jump, on silence. Not chased
further.
