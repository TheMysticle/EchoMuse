# Local-first versus Gemini Live, 2026-10-18

Local commands beat Gemini Live in this controlled comparison. LLM fallback
does not yet meet parity. Median milliseconds from the end of speech to the
first response audio chunk, three trials per case:

| Request | Local-first | Gemini Live |
| --- | ---: | ---: |
| Turn off Quest Light in the bedroom | 566 | 1,251 |
| What's the weather like | 664 | 1,277 |
| Resume the Bedroom Speaker | 681 | 1,440 |
| Tell me one short joke (LLM fallback) | 1,409 | 755 |

The first three requests used HA's native local agent. Live called the real
HA MCP light, weather and media-unpause tools. For music, the bedroom player
was paused before every trial; it was verified playing after the final resume
and paused again after testing. Unqualified “play music” sometimes made Live
ask a clarification, so those exploratory timings are excluded.

The composed route used real Pipecat 1.4 Silero VAD, Sherpa STT, the native HA
router, Gemini 3.5 Flash Lite on fallback, and Piper Wyoming. Live used Gemini
3.1 Flash Live Preview and native generated audio. HA ran at 192.168.1.47;
Sherpa and Piper ran at 192.168.1.211. Both received identical, paced,
Piper-generated speech for each phrase and the same 34 real HA MCP tools.
The composed session and HA connection pool persisted across turns. Its
first trial includes creation of the HTTP client. Live sessions were ready
before timed speech and received an explicit audio-stream-end marker.

This is a component benchmark from the Windows development machine, not a
physical satellite round trip. It excludes microphone capture, controller
transport, device endpoint processing, output prebuffering and speaker
playback. Both models used the configured base brief-answer instructions;
satellite-specific instructions and scheduler tools were omitted. Small
sample counts and cloud variability limit precision. The raw measurements
and further method notes are in [the JSON artifact](pipecat-latency-2026-10-18.json).

The fallback joke's median first model text arrived around 1,198 ms after
speech, while routing started around 403 ms. The HA no-match check took about
10 ms and Piper first audio followed first text by about 183–211 ms on warm
trials. The cloud text-model leg is the largest remaining delay, rather than
HA local recognition. More locally handled commands improve the common case;
these results do not establish Live parity for arbitrary LLM requests.

The investigation also fixed three problems: creating a fresh HA HTTP client
for every turn, an extra 200 ms policy timer after VAD had already closed a
Sherpa utterance, and a router crash on Gemini's `LLMSpecificMessage` history
objects after the first real model response. Sherpa now collects server
segments until `Done!`, so eliminating the extra timer cannot execute an
earlier segment as a complete command. Gemini Live's runtime settings were
not changed.

Reproduce with `tools/experiments/pipecat_latency_probe.py`, the generated
Pipecat add-on folder, Pipecat Google/MCP extras, and Wyoming installed.
Supply credentials through `ECHOMUSE_GOOGLE_TEST_KEY` and
`ECHOMUSE_HA_TEST_TOKEN`; the script does not save them. Test phrases can
execute real HA actions and must be authorized for the target devices.
