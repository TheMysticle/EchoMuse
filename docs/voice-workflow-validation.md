# Pipecat Home Assistant live validation

Verified locally on 2026-10-17 against upstream commit
`a538d1d1bacc8609061719f0b05c1622147ccb4b` of
[kyvaith/pipecat-homeassistant](https://github.com/kyvaith/pipecat-homeassistant).

| Component | Tested configuration |
| --- | --- |
| Home Assistant | 2026.10.1, Python 3.14, isolated config |
| Pipecat Assist | Source installation, Pipecat 1.4.0, Python 3.12.13 |
| Model | `gemini-3.1-flash-live-preview`, real Gemini Live connection |
| HA | `http://127.0.0.1:18123` |
| Pipecat audio | `ws://127.0.0.1:7860/api/assist/esphome` |
| MCP | `http://127.0.0.1:18123/api/mcp`, authenticated |
| Satellite | Simulated hardware/control socket; real data WebSocket and EchoMuse routing |
| Entities | Two simulated light devices in `Voice Test Room`, one exposed to Assist and one hidden |

The Pipecat Assist integration loaded successfully in HA and registered its
Conversation, STT, TTS and AI Task entities. This standalone test uses an HA
long-lived token; it does not emulate Supervisor or test add-on installation.

## Results

| Check | Result |
| --- | --- |
| Upstream protocol/provisioning tests | 25 passed |
| EchoMuse workflow regression tests | 43 passed after the wake-gating fix |
| Dashboard JSX | esbuild compilation passed |
| Full controller suite after wake-gating correction | 2,292 passed, 27 skipped; three existing firmware/emOS failures also reproduced on the unchanged baseline |
| MCP schema | Seven tools registered, including GetLiveContext and turn-on/off |
| Entity exposure | Exposed light appeared with its area/state; hidden light absent |
| MCP control | Exposed light could be switched; hidden-light request rejected and its state stayed off |
| Provider setup | Wizard API updated the real server's Gemini key while preserving the HA MCP integration |
| Speech to HA | Synthetic spoken request reached Gemini through EchoMuse and Pipecat; Gemini called HA MCP and turned the test light on |
| Full duplex / barge-in | Second spoken request started while returned audio was arriving; Pipecat interrupted speech and Gemini turned the light off |
| Concurrent audio | 106,496 returned PCM bytes; 16 microphone frames sent while output was arriving |
| Session integrity | Native-mode switch preserved the satellite data socket and registration; 45 control pings recorded |
| Temporary provider key | Removed from local test files/config/logs; Pipecat restarted without it |

The control socket is a test double. Physical ALSA capture/playback, physical
buttons/LEDs, acoustic echo cancellation and device Wi-Fi behavior were not
hardware-tested. The test verifies actual controller data routing, remote
Pipecat transport/pipeline, cloud model inference, and HA MCP execution.

The live Gemini results above were captured before the wake-gating correction.
The temporary provider key was removed; those cloud tests were not repeated
for the correction. The updated harness now simulates an on-device wake and
numbered microphone frames rather than continuous ambient audio.

The wake-gating regression tests use real local WebSockets and the controller's
wake listener without a native HA satellite connection. They verify the chosen
detector/model remains configured, idle audio and unsolicited output are
suppressed, early numbered audio waits for its matching wake, stale and wrong
sessions are dropped, full-duplex audio works after a wake, terminal phases and
deadlines close the gate, and another wake opens a new conversation. They also
cover button capture restoration, the first partial speaker frame after a wake,
exponential retry backoff, and persistent satellite registration/data sockets.

## Reproduce

Install upstream Pipecat Assist into a Python 3.12 environment and Home
Assistant 2026.10.1 plus its normal component requirements into Python 3.14.
`tools/experiments/ha_pipecat_bootstrap.py` starts an isolated HA runtime,
installs the Pipecat custom component from a source checkout, creates the
simulated lights, and stores a test-only token with mode 0600:

```bash
/path/to/ha-python tools/experiments/ha_pipecat_bootstrap.py \
  --work-dir /tmp/echomuse-pipecat-integration \
  --pipecat-source /path/to/pipecat-homeassistant
```

Start the Pipecat FastAPI app on loopback port 7860 with
`PIPECAT_ASSIST_DATA_DIR` pointing to the test data directory, `HA_MCP_URL`
pointing to the test HA MCP endpoint, and `LONGLIVED_TOKEN` loaded from
`ha-token`. Pipecat must be available when HA adds its integration; on first
setup HA may need to be restarted after Pipecat starts. Configure a Gemini Live
provider and retain the server's satellite shared secret in a private file.
Prepare two mono 16 kHz PCM16 WAVs: ask to turn on the exposed test lamp and
count slowly; then ask to stop counting and turn it off.

Run the live test using the controller's Python 3.12 test environment:

```bash
/path/to/controller-python tools/experiments/ha_pipecat_workflow.py \
  --ha-token-file /private/ha-token \
  --satellite-key-file /private/satellite-key \
  --google-key-file /private/temporary-google-key \
  --audio /tmp/turn-on.wav \
  --barge-audio /tmp/barge-off.wav \
  --report /tmp/live-workflow-report.json
```

`--google-key-file` is optional if the server already has a provider key. No
secret is embedded in the command, source or report. Use isolated test entities;
the script changes `light.exposed_test_lamp` and checks
`light.hidden_test_lamp` remains off. Remove temporary provider credentials and
stop/restart test services after testing to clear them from memory.

## Native playback parity, continuation and scheduler adapter

The subsequent playback correction shares native Assist's streaming output
processing, pacing, device playback acknowledgments and refreshed scene meter.
Regression tests check reference-counted ducking and speaker ownership until
hardware drain, thinking/spoken scenes, continued uplink during a reply, reply
cancellation without releasing the turn's duck, and cleanup after mute, native
switch and Pipecat disconnect. Normal replies close the uplink; marked
continuation questions retain it. Background input is suppressed during the
thinking phase, and the configured wake barge-in setting is preserved.

The adapter contract test uses the real Pipecat MCP client and registered LLM
tool callbacks, authenticated local HTTP, the controller schedule bridge and
the real Go scheduler. Each device command runs in a new process against the
same checksummed state file, verifying that created timers, alarms and reminders
survive reopen. It also checks cancellation, rejects model-supplied speaker
redirection, instantiates Silero with the balanced profile, and exercises the
packaged FastAPI WebSocket route's authentication/feature negotiation.
The route test substitutes the model runner: it makes no cloud API call.

Reproduce after generating the Pipecat adapter folder:

```bash
(cd device && go build -o /tmp/echomuse-schedule-contract ./tools/schedule_contract)
/path/to/pipecat-python tools/experiments/pipecat_scheduler_contract.py
```

These checks do not measure physical duck depth, acoustic barge-in rejection or
LED rendering on real hardware. The user needs both updated add-ons for session
sensitivity controls, continuation markers and local scheduler model tools.

The combined workflow/playback/scheduler/adapter regression set passed **72 tests**.
Final controller suite for these changes: **2,322 passed, 27 skipped**, with
the same three pre-existing firmware/emOS failures listed above. Dashboard JSX
compilation, new-module lint, scheduler/clock Go tests, and the real Pipecat
adapter/scheduler contract checks passed. No new Gemini/cloud conversation was
run for this correction.
