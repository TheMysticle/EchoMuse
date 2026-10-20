# Speaker groups: test record

Tested on two Echo Dot Gen 2 units on 2026-10-19 and 2026-10-20: **015G**
(leader) and **05HU** (member), both on emOS, controller running as the Home
Assistant add-on. This page says what was checked, what the result was, and
what has not been checked. Pause fix and sync confirmed by ear on 2026-10-20. Guide for users: [speaker-groups.md](speaker-groups.md).

## Result

| Question | Result | Source |
|---|---|---|
| Does Spotify play in sync on both Echoes? | Yes, "works very well" | Listening, owner |
| Delay when a song starts | Not audible | Listening, owner |
| Delay when playback stops | About 2 s on first test. Cause found and fixed (below); pause confirmed working on `b63fbab` | Listening, owner |
| Sync between the two Echoes | "Sounds very good" | Listening, owner |
| Does anything change with no groups configured? | No difference measured | Docker A/B run |
| Do wake word and Assist work on the grouped Echoes? | Yes | Controller log |

## Stop delay and the fix

The first hardware test showed a stop that played on for about 2 s, or
dropped out and came back for those 2 s. After a pause, librespot flushes its
last audio periods on the audio socket, and the leader read them as playback
starting again, so it replayed the 2 s it had kept for resume. Fixed in
`b63fbab`: while Spotify is paused, trailing audio is added to the resume
buffer and playback does not start. A host test failed before the fix
(94,080 samples played after a pause) and passes after (0).

On the host, a stop is heard about 47 ms after the event on both leader and
member. Real WiFi can add to that on the member, and no figure for it exists
yet.

## Field log review (2026-10-20)

Read from the add-on log through the Home Assistant API: 3,000 entries,
19 Oct 04:43 to 20 Oct 10:33. Both Echoes were running `b63fbab` from about
10:16 and 10:18.

- The controller linked 05HU to leader 015G at 03:21:36 (one `[group] linked`
  line) and logged no group error afterwards.
- No `ERROR` line and no traceback in the retrieved window.
- Wake word on the leader after the update: three turns at 10:21, 10:27 and
  10:30, scores 0.879 to 0.995, wake-to-arrival age 23 to 32 ms. Before groups
  existed the same Echo showed 22 to 72 ms. Each turn ended with a completed
  reply.
- Wake word on the member: one wake at 10:25 (score 0.885, age 1172 ms) that
  ended in a listen timeout. The same Echo logged 1475 ms on 19 Oct 12:00,
  before groups, so the age is not new. The member's age varies between 50 and
  220 ms in the rest of the log.
- Three event-loop stalls of 371 to 464 ms, all while a firmware binary was
  being uploaded to an Echo (03:19, 03:20, 10:16). None during playback or a
  voice turn.
- 223 "Pipecat audio unavailable" warnings, all from 19 Oct while the Pipecat
  add-on was restarting, before groups.

## No groups configured: A/B against the previous controller

Real Home Assistant (stable) in Docker, the controller from `1c79066` (before
groups) against the current one, a simulated Echo client, instant stand-in
STT and TTS. Three alternating rounds each: 36 Assist turns, 15 music
play/stop cycles and a 45 s idle mic stream per side.

| Path | Before | After |
|---|---|---|
| Button to mic open, median / p95 | 0.7 / 1.0 ms | 0.7 / 0.8 ms |
| Button to LEDs, median / p95 | 0.8 / 1.1 ms | 0.8 / 0.9 ms |
| End of speech to first reply audio, median / p95 | 38.4 / 40.9 ms | 37.7 / 42.4 ms |
| Home Assistant play to first music frame, median / p95 | 89.5 / 90.8 ms | 88.8 / 92.0 ms |
| Stop to flush signal, median | 1.73 ms | 1.76 ms |
| Controller CPU, idle mic stream | 5.1% | 5.1% |

On the Echo, the ring hooks the leader adds cost about 4 to 6 ns per 20 ms
period with no allocations (`internal/spotify/ring_bench_test.go`).

## Not checked

- **Sync offset.** Sync was judged by ear only; no measurement of the offset
  between the two speakers exists.
- **Stop tail on WiFi.** The owner confirmed pause works on `b63fbab`, but the
  delay on the member over a lossy link is unmeasured. If a stop ever feels
  late, the `[group] stop: Nms of audio had been sent ahead` line in the
  leader's log shows how much audio was queued.
- **Device logs and group status**, which the controller serves to a signed-in
  dashboard session only. This review used the add-on log.
- **Pipecat** in the Docker run, and the Echo's mic and speaker hardware,
  which a simulated client cannot exercise.
- **More than two Echoes**, and left/right assignment on hardware.
