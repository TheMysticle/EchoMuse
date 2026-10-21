# EchoMuse voice startup buffer, 2026-10-21

## Findings

The bedroom Echo waits for 24 periods of voice audio before playback: 2048
samples per period at 48 kHz is 1024 ms of audio. EOS releases shorter replies.
Pipecat output is delivered at approximately realtime in the observed turns;
this therefore adds almost one second after response audio already exists.
The dashboard's Speaking state starts at the controller's first send and is
not a device playback-start acknowledgment.

The initial 50-row bedroom activity snapshot contained 46 playback reports.
Their median `prime_wait_ms` was 957 ms (range 1–996 ms). The very short waits
are possible when all audio is already available or EOS releases the gate.
This is first packet received to first period consumed, not an acoustic DAC
measurement. ALSA buffering remains downstream of this measurement.

The user's installed candidate firmware advertises `voice_prebuffer` and the
controller reports `voicePrebufferMs=512`. Four completed subsequent turns:

| Turn | Startup wait (ms) | Underruns | Minimum queue depth | Largest arrival gap (ms) |
| --- | ---: | ---: | ---: | ---: |
| 1273 | 443 | 0 | 10 | 81 |
| 1274 | 472 | 0 | 11 | 80 |
| 1275 | 454 | 0 | 10 | 80 |
| 1282 | 444 | 0 | 10 | 80 |

Their median is 449 ms, 508 ms below the initial historical median. These are
observational samples with different utterances, not a randomized paired
benchmark. Two other subsequent sessions (1278, 1283) ended as `device_ended`
after about 30 seconds with `tts_url_ms=-1` and no playback statistics. They
received no response audio; investigate upstream logs separately. Do not count
them as zero-latency samples or claim the entire pipeline is regression-free.

A separate replay of the user's 1.76-second bedroom-light recording through
the real configured Pipecat WebSocket produced its first audio 581 ms after
the file ended, recognized the command correctly, and returned 1.92 seconds
of 24 kHz PCM. This probe collected output on the test host; it did NOT route
that reply through the physical satellite. File end is not an acoustically
annotated end-of-speech timestamp. It lacks the production scheduler header.

## Settings audit

The deployed Pipecat flow uses Deepgram nova-3, Cerebras gpt-oss-120b, HA MCP,
and Cartesia sonic-3, with sentence TTS aggregation and local fast-path routing
enabled. Some generic flow fields still name Gemini; composed steps select
the actual integrations. Do not interpret those generic fields as the active
Cerebras model or Silero configuration.

Pipecat advertises these values in its `va-pipecat` hello:

- playback prebuffer: 300 ms;
- follow-up listening: 30000 ms;
- follow-up microphone delay: 80 ms;
- wake microphone delay: 0 ms.

EchoMuse's `em_workflow.handshake` does not consume these timing fields. They
are interpreted by upstream ESPHome clients, not by this controller's PCM
bridge. Changing that prebuffer slider does not change the Echo's firmware
gate. EchoMuse deliberately ends ordinary responses; it retains listening
for an explicitly requested clarification. This behavior must not be removed
as a latency optimization. Earlier fixes also preserve partial PCM tails,
keep MCP connections pooled, and prewarm the resampler; retain them.

The new **EchoMuse Playback → Voice playback buffer** setting is independent
of Pipecat's slider. It applies to the Echo's voice plane (including native
Assist and announcements), not music. The default remains 1024 ms. The UI
offers 256/512/1024 ms; the API accepts integers 128–1024, rounded up to whole
2048-sample periods in firmware. Invalid values are rejected; absent updates
preserve the current firmware setting. Old firmware retains its original gate.
The controller's response meter estimate follows the chosen gate only when
the device advertises support. Dashboard Speaking is still an estimate.

## On-device Pipecat

The actual bedroom device is Radar on emOS with an armv7 kernel. At inspection
it had 481 MiB total RAM, approximately 412 MiB MemAvailable, a 3 ms controller
RTT, strong 5 GHz reception and reported zero recent link loss. Memory alone
does not establish that Pipecat cannot run. Python/native dependency support,
CPU contention with audio, and sustained resource use need separate testing.
The observed ~1-second speaker gate is much larger than the measured LAN RTT;
placing the orchestrator on the Echo would not automatically remove that gate
or any cloud STT/LLM/TTS/tool round trips.

## Validation and continuation

- Speaker/config Go regression suites passed with race detection.
- 223 targeted controller tests passed (config typing/scoping, adapter,
  meter gate and deployment guards).
- ARM server build succeeded; JSX compiled with esbuild 0.20.2.
- The user installed the build and selected 512 ms. Live metadata showed both
  bedroom and living-room devices on the new build/setting. Tests and command
  authorization remain limited to the bedroom; no living-room actions ran.

Next: controlled repeated A/B trials at 1024/512/256 ms with identical input,
including short replies, long replies, music ducking, interruptions, subsequent
turns and cloud idle reconnection. Capture acoustic output if claiming exact
speech-to-audible latency. Check the two no-audio turns before claiming broader
reliability. No changes to Pipecat provider settings were made in this audit.

Private raw snapshots, credentials, captured response audio and the temporary
Python harness are under `/tmp/echomuse-latency` on the test machine. Credentials
and recordings must not be committed. The harness uses HA ingress sessions;
refresh them after expiry/restarts. Its `read` command saves fresh snapshots,
so copy a baseline before collecting another. Temporary transfer service uses
port 18937 and serves only its `transfer` subdirectory; stop it when finished.
