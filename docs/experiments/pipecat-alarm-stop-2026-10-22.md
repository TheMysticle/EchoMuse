# Alarm-stop failure, 2026-10-22

## Evidence

Sources: the supplied device log and Pipecat add-on log ending
`2026-10-22T11-00-50.806Z.log`. Times below are the matching 12:00
timestamps inside the logs, not the export filename's UTC timestamp.

- 12:00:02: speaker playback starts.
- 12:00:10: wake score 0.963 opens device session 26. Pipecat receives
  16 kHz audio, initially peak 32765 / RMS 9886.6, then much quieter audio.
- 12:00:10–18: Silero stays QUIET. Every diagnostic window reports zero
  frames meeting confidence 0.7; window peaks include 0.59 and 0.69.
  Volume exceeds the separate minimum-volume gate. There is no user-speech
  start, transcript or alarm-stop tool invocation in this interval.
- 12:00:15: a second wake scores 0.981 but firmware ignores it inside the
  already-open session.
- 12:00:18: the controller closes session 26, consistent with its eight-second
  listening timeout when no transcript or thinking event arrives.
- 12:00:20: Gemini reports connection error 1008 and reconnects. This is
  after the listening failure; it does not explain the zero VAD detections.
- 12:00:29: a physical button event is followed by speaker-off.

The immediate failure is upstream of intent/tool handling: the local speech
gate never starts a user turn. The log shows Gemini Live for this connection;
settings observed on a different composed connection do not describe this turn.

The firmware endpointer also has no usable reference (`warm=false`). It
excludes the speaker's own playback from its background history, leaving too
little clean history during the alarm. Its subsequent `+Inf` and `heard=true`
diagnostics arise from division by a zero reference; `Warm()` / `Ended()`
guards prevent an endpoint marker. This does not block audio transmission.

## Change and limits

The EchoMuse Responsive profile now uses confidence 0.5 instead of 0.7,
retaining start_secs=0.12, min_volume=0.02 and live stop_secs=0.3. Balanced
and Low profiles are unchanged. 0.5 is also the default threshold in
[Silero's reference implementation](https://github.com/snakers4/silero-vad/blob/master/src/silero_vad/utils_vad.py).
This is a sensitivity tradeoff: it can admit more background speech/noise.
It does not bypass classification or stop an alarm merely on a wake word.

VAD diagnostics now include the longest consecutive confidence-qualified
run in milliseconds, alongside the required start duration. This run is
confidence-only: the volume gate and state machine still decide speech.
Peaks alone cannot tell whether a lower threshold has sufficient duration.

No recording or per-frame confidence trace was supplied. The logs cannot
prove which frames were the user's command, whether it was acoustically
damaged, or that this threshold alone would have recovered the exact utterance.
Alarm echo, the microphone beam and the initial near-full-scale audio remain
possible contributing factors, not established root causes.

## Validation and rollout

Regression tests exercise Pipecat 1.4.0's real VAD state machine with controlled
classifier scores: sustained 0.59/0.69 speech, sub-threshold 0.49 background,
and an isolated 0.69 peak. These are synthetic decision tests, not replay of
the incident. The existing real-Silero test checks that diagnostics do not
alter decisions. Adapter tests also cover session isolation and interruption
policy.

Results: 55 adapter tests and all 5 VAD runtime tests passed; `git diff
--check` passed. The runtime tests required execution outside the sandbox
because its asyncio executor wakeup stalled; the unsandboxed run took 1.07s.

The source adapter and existing local generated add-on copy are updated.
No running add-on or device has been changed. Rebuild/reinstall the Pipecat
EchoMuse add-on with this adapter and reconnect the satellite to construct
the new VAD. Confirm `bar=0.5` on a Responsive session, then test short stop
commands with the alarm ringing, silence after wake, ordinary longer requests
and background speech. Check both the transcript and successful EchoMuseStop
or alarm-dismiss result. Firmware/controller deployment is not required for
this change. Changing only a flow's generic VAD setting may not affect this
connection: the EchoMuse adapter supplies its own profile.
