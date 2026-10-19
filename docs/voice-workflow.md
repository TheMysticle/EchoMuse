# Per-satellite Voice Workflow

Open a satellite's **Config → Voice Workflow**. Home Assistant Core is the
default and retains the existing native ESPHome/Assist audio path. Select
**Pipecat Assist**, enter a `ws://` or `wss://` endpoint and credentials, then
choose **Verify & activate Pipecat Assist**. The controller performs the
WebSocket upgrade and waits for a pong before saving. For Pipecat Home
Assistant it also verifies the server's versioned PCM `hello` message. A failed
connection test leaves the saved workflow and current audio route unchanged.

Pipecat mode keeps the selected on-device wake detector and model enabled.
Connecting to the server does not send microphone audio: only a wake word or
an explicit action-button request opens a conversation. The controller forwards
16 kHz mono signed 16-bit little-endian PCM for that session. Ambient frames
and frames from other or closed sessions are dropped. After the user finishes
speaking, upstream microphone transmission pauses while the assistant thinks.
What happens during the reply is the **Interrupt a reply by** setting.
A normal completed reply closes the microphone gate before music returns to
normal volume. Only a continuation question keeps the conversation open. Device
session end, hardware mute, disconnects and workflow changes also close the
gate.

### How a session ends

The server says when a reply is finished, but the controller does not depend on
it: every wait has its own limit, restarted by each message the server sends.

| Waiting for | Limit | Log reason |
| --- | --- | --- |
| The user to start or keep speaking (after a wake, or a clarification question once its reply has been heard) | 8 s | `listen_timeout` |
| The server to answer, microphone gated (tool calls included) | 30 s | `think_timeout` |
| The server to continue or finish a reply it has started | 10 s | `reply_stalled` |
| The whole conversation, continuations included | 120 s | `session_max` |

A server that finishes a reply and goes quiet ends the session after the audio
it already sent has played out; a stalled server does not cut speech off. A
clarification's 8 s starts when the question has been *heard*, not when the
server stopped sending it, because a paced speaker plays several seconds behind
the network. Partial transcripts count as server messages, so a long command
keeps the window open for as long as it is being spoken. Each end is logged as
`Pipecat session N ended: <reason> after <seconds>`, which says which limit
closed a conversation that stayed open longer than expected. An action-button session temporarily uses diagnostic
capture while retaining the classifier, then restores the selected capture mode.

Switching workflows leaves control, data, telemetry, buttons and Home Assistant
entity connections registered. Both workflows use the same response playback path: streaming EQ/limiting,
output pacing, the selected listening/thinking scenes, and the audio-reactive
speaking meter. Music uses the existing reference-counted ownership: capable
speakers duck locally; older firmware pauses/resumes. Ownership and the meter
remain held until the speaker reports playback completion, rather than ending
when the last network packet is sent. A barge-in flushes only the reply and
keeps music ducked for the ongoing turn. Beam locking and on-device wake
arbitration are retained.

Pipecat failures retry with exponential backoff
from one to 30 seconds, without repeated idle speaker flushes; they never
silently send speech to Home Assistant instead. The dashboard reports whether
the connection is waiting for a wake word or handling a conversation, together
with a redacted error code when the audio connection fails.

## Pipecat Home Assistant

Install [kyvaith/pipecat-homeassistant](https://github.com/kyvaith/pipecat-homeassistant)
and its HA custom integration according to that project's instructions.
For the controller's direct full-duplex connection use:

```text
ws://<pipecat-host>:7860/api/assist/esphome
```

Enter the add-on's **satellite shared secret** as the Server API key. EchoMuse
sends it as a Bearer header rather than placing it in the endpoint URL. A
`flow_id` query parameter can select a particular pipeline. EchoMuse detects
this endpoint automatically, verifies `va-pipecat` protocol version 1, and sends a
`wake` event immediately before the first PCM frame of each awakened session.
The standard server negotiates 16 kHz mono
input and 24 kHz mono output. Returned JSON phases drive dashboard state and
LED controls; `interrupt` flushes buffered speech. An action-button tap opens an
idle session, interrupts speech in an active Pipecat session (which keeps
listening for the answer), and otherwise, while it is only listening or waiting
for the model, ends the session.

Configure the model key in the add-on, or enter a new Google AI Studio key in
the EchoMuse wizard. For this server, an explicitly entered key updates the
Gemini integration through its `/api/assist/config` API before connection
verification. This changes that server's Gemini integration, shared by its
pipelines; it is not a per-satellite model credential. The update preserves
other integrations and their existing secrets. A later connection failure does
not undo a provider key already applied on the remote server.

### EchoMuse session adapter: speech sensitivity and local schedules

The pinned Pipecat adapter is generated by:

```bash
python3 tools/pipecat_echomuse/package.py --source /path/to/pipecat-homeassistant
```

The source checkout must be at commit
`a538d1d1bacc8609061719f0b05c1622147ccb4b`. The script verifies its adapter
anchors and writes `dist/pipecat-assist-echomuse-dev`, a local add-on that builds
from source. Copy that folder and `dist/echomuse-dev` into HA's `/addons/`,
reload the add-on store, and build/rebuild them. The controller's existing dev
slug is retained. Pipecat's local adapter has its own slug/storage: stop the
original Pipecat add-on and copy its `/data/pipecat_assist.json` into the new
add-on's `/data/`, or re-enter its provider/MCP settings and the same satellite
shared secret. Run only one Pipecat add-on on port 7860. The endpoint URL stays
`ws://<HA-host>:7860/api/assist/esphome` when hosted on the same machine.

### Cerebras Cloud

In the Pipecat add-on, add a **Cerebras Cloud** integration, enter its API key,
and select it for a composed pipeline's LLM step. The default model is
`gpt-oss-120b`; the model field remains editable and model discovery reads
the account's available IDs from `GET https://api.cerebras.ai/v1/models`.
The default base URL is `https://api.cerebras.ai/v1`, with streaming chat
requests sent to `/chat/completions`. A custom proxy base URL is optional.
Standalone deployments can supply `CEREBRAS_API_KEY` for the default integration.

Cerebras uses the existing OpenAI-compatible streaming and tool-call path,
including HA MCP, and also supports the add-on's HA Assist text-conversation
route. It supplies the LLM step; select separate STT and TTS integrations.
Gemini Live remains a separate realtime pipeline.
Endpoint reference: [Cerebras OpenAI compatibility](https://inference-docs.cerebras.ai/resources/openai).

### Local commands before the LLM

Composed Deepgram STT explicitly disables profanity filtering. Pipecat 1.4
otherwise enables it by default, which can censor literal song titles before
local HA matching or LLM inference. Gemini Live and HA Assist's separate STT
HTTP route retain their existing configuration.

For composed pipelines, the Echo's own end-of-speech marker closes the uplink
and switches the ring to thinking immediately, independently of STT latency.
The controller still sends the endpoint silence needed to finalize recognition.
This behavior is negotiated by the add-on; realtime Gemini Live is unchanged.
Sherpa preserves the audio arriving before VAD confirms a new utterance, so
short commands after the first request retain their initial words. An empty
recognition asks for a retry and cannot rerun the previous conversation.
Sherpa server segments are collected until `Done!` seals the utterance. Its
aggregator waits for that finalized transcript without adding a second silence
timer after VAD. Other STT services retain their existing timeout.

In composed STT/LLM/TTS pipelines, fast-path routing first submits the complete
user utterance to Home Assistant's built-in `conversation.home_assistant` agent.
This stays on the HA host: it does not select your configured cloud agent.
HA handles its own built-in commands and state queries, custom sentences,
`intent_script` responses, and sentence-triggered automations. Only
`no_intent_match` or `no_valid_targets` falls through to the configured LLM. An unavailable or
unauthorized local API, or a request rejected by HA's schema validator, also
permits fallback before any action is dispatched.
A handler error or an
uncertain network response is handled locally so an action cannot be repeated
by the LLM after it may already have succeeded.
The HA HTTP connection pool is reused for the satellite session and closed
when the pipeline is cleaned up. Gemini-specific conversation-history objects
are preserved and skipped when counting new user messages.

The add-on uses its existing Supervisor API access, or the configured core
`/api/mcp` URL and HA token for standalone deployments. The controller sends
the satellite's stored ESPHome MAC; the add-on resolves that to HA's device
registry ID so room context and satellite-owned commands reach the right
speaker. The workflow's session device ID remains a conversation-memory key.

Install custom sentences in HA's `/config/custom_sentences/<language>/` and
configure their handlers in HA as usual, then reload HA's conversation data.
Sentence-triggered automations remain managed in HA. Reading YAML in Pipecat
alone does not install an HA intent handler. Local replies use the configured
TTS service, including Piper, and are added to conversation history.

Before LLM fallback, the add-on's HA-compatible `weather.yaml` sentences
handle today's/tomorrow's forecasts, the day after tomorrow, named weekdays
and rain questions locally through the configured `script.get_weather` MCP
tool. The script's `daily_forecast` must contain the existing weekday-labelled
high/low/rain format. Relative days use HA's cached timezone; malformed or
missing data falls back to the configured LLM. Future-weather requests cannot
be satisfied by HA's current-conditions response. Current-weather questions
retain the native HA route. Copy the supplied `custom_sentences/en/weather.yaml`
into HA's `custom_sentences/en/` directory for this Pipecat handler; native
HA Assist is unchanged.

Ordinary composed voice pipelines use the shared dependency-free
`tool_selector.py` to advertise relevant lighting, music, weather, climate,
cover or schedule tools for recognized English clauses that need the LLM.
Multiple clauses expose the union of their categories. Clear self-contained
chat omits the HA catalogue, retaining only configured conversation continuation
and tool expansion. Indirect home questions and uncertain chat retain tools.
Those same clear chat phrases bypass the native HA conversation attempt;
pending local schedule clarifications still take precedence.
The model can
call `EchoMuseExpandTools` to restore all configured/allowlisted tools if needed.
Ambiguous, conditional and partly unsupported requests retain the full set. Local HA
responses still bypass the LLM entirely. Gemini Live, flow graphs and the
separate HA Assist text HTTP route retain their existing tool handling.
See [the tool-scope audit](experiments/pipecat-tool-scope-2026-10-19.md).
The [local preview](../tools/tool_selector_preview/README.md) runs the exact same
selector and accepts editable tool catalogues. Tool-result continuations keep
the current selection; expansion restores only the configured allowlist.

Composed ESPHome output pads the last partial 40 ms TTS packet before reporting
speech completion. This prevents Pipecat 1.4 from discarding the final PCM
samples when it clears its output buffer. Padding adds less than 40 ms of
silence at the end, with no first-audio delay. Real interruption still discards
pending audio. Gemini Live uses its unchanged output path. The controller's
normal end-of-session `stop` can produce `InterruptionWorkerFrame` log entries;
those entries alone do not mean VAD barge-in or prove that playback was cut.

Flows with an explicit MCP tool allowlist retain the restricted hassil/MCP
matcher rather than gaining access to the broader conversation API. This is
also the fallback when no native HA API credentials are configured. The Custom
Sentences tab labels that fallback separately. Gemini Live is unchanged.

### Interrupting a reply

**Wake word (default).** The microphone is closed to the server from the first
reply audio until the reply has been heard, so the speaker's own voice cannot
be taken for the user and cut the reply off. The Echo keeps listening for its
own wake word, and the action button works too; either interrupts the reply and
opens a new session. A clarification question reopens the microphone once the
question has finished playing.

**Voice.** The microphone stays open under the reply and the server's detector
decides, so you can interrupt without the wake word. The cost is that the
speaker's echo can trigger it. Use the Balanced or Low profile with it.

Wake-word interruption needs an Echo that detects its own wake word ("On this
Echo"). One that streams to the controller has nothing scoring a wake during a
session, so it uses Voice whatever the setting; the dashboard says so.

### What the Echo's audio processing does here

The audio sent to the server is the Echo's wake stream, so the beamformer and
the adaptive interference canceller apply whenever `wakeBeam` is on, and echo
cancellation as everywhere. The controller's `nsAsr` noise suppression and its
speech gate belong to the native Home Assistant path and are not applied.

With **Endpoint** on (`wakeEndpoint`, Config → Wake word), the Echo detects that
the talker has stopped, which a TV playing nearby otherwise hides from the
server's own detector. A Pipecat server has no message for "the user is done"
and ends a turn by hearing quiet, so the controller sends it 0.6 s of digital
silence after the speech and then closes the uplink until the reply has been
heard. The log line is `the Echo heard the talker stop`. Without a warm
reference the Echo sends nothing and the server decides alone.

### Speech detection

The profile sets how long the server must hear speech before it counts you as
speaking. One-word commands such as *stop* and *play* are shorter than the
Balanced wait, so the server may never notice them; Responsive hears them. It
defaults to Responsive with wake-word interruption (the closed microphone leaves
no echo to trip it) and to Balanced with Voice. A stored choice is kept.

| Profile | Speech confidence | Sustained speech | Minimum volume |
| --- | --- | --- | --- |
| Balanced (default) | 0.80 | 300 ms | 0.04 |
| Low sensitivity | 0.85 | 450 ms | 0.06 |
| Responsive | 0.70 | 120 ms | 0.02 |

These tune the Gemini Live local Silero detector; Gemini server VAD is disabled
when that local detector drives turns. They do not change the selected wake
model's threshold or override its barge-in preference. Profiles are starting
points for room tuning, not measured acoustic guarantees. Other model providers
retain their existing detector configuration.

The adapter advertises a per-satellite `EchoMuseRequestContinuation` tool. The
model calls it before asking for a required missing detail; the tool call sets a
flag on that satellite's own protocol session, and the completed reply carries
that explicit flag back to the controller. A question mark alone does not keep
the microphone open. Normal answers return to wake-word standby, while a
requested clarification opens the existing bounded continuation window. The
prompt tells the model to confirm a finished request in a few words and stop,
without closing offers such as "Can I help with anything else?", and to use the
tool only when it needs an answer to complete the request. A prompt is guidance
and not a filter: a flow instruction of your own that asks it to check for
further requests will override this, so look there if the habit returns.

Every authenticated raw-PCM EchoMuse session also gets the satellite-scoped
`EchoMuseStop` tool, which handles a bare "stop" by stopping **one** thing, in
this order:

1. Whatever is ringing: a timer is cancelled, an alarm or reminder dismissed.
2. Otherwise this speaker's music: Spotify Connect, and Home Assistant audio
   streamed through the Echo.
3. Otherwise a countdown timer. Alarms and reminders set for later are never
   touched.

So a timer going off while music plays is silenced and the music keeps playing;
a second "stop" stops the music. A countdown that has not rung is not cancelled
by a "stop" that stops music, since "cancel the timer" is the way to say that.
The tool reports `stopped`, `left_running` and `errors`, and the model is told
to leave `left_running` alone. Only when nothing was stopped here does it look
for another playing media player in the speaker's mapped area and pause it with
`HassMediaPause`.

The same session also gets `EchoMuseVolume`, for a bare "volume 20" with no
named device or room. It sets the level on this speaker alone: Home
Assistant's own volume tool, given only an area, applies to every matching
media player it finds there, which can also change a TV's volume in the same
room. `EchoMuseVolume` sends the change straight to the physical device and
never goes through Home Assistant's own targeting, so a different device's
volume still needs the user to name it or its room.

Enable **Connect this speaker’s persistent timers, alarms and reminders** to
attach three additional tools to this authenticated Pipecat session only:
`EchoMuseCreate`, `EchoMuseList` and `EchoMuseAction`. The controller supplies a separate scoped
key and its advertised LAN API URL to the server in WebSocket headers. The
server must be able to reach that URL. No additional HA permission, shared
all-speaker tool integration, companion-key rotation, or firmware update is
needed if the speaker already supports local schedules. A missing adapter or
unconfigured satellite shared secret fails verification for these options.

Model arguments cannot choose a speaker. Tools use the same acknowledged
schedule transport and persistent speaker storage as the dashboard. Creation
accepts durations or explicit offset-qualified dates, timezones and recurrence;
listing reports current UTC time and the configured controller timezone (or a
single timezone already present in the speaker’s schedules); an unknown
calendar timezone requires clarification; actions address
exact item IDs. Invalid/ambiguous input, offline speakers and failed device
acknowledgment return errors rather than confirming a save. Native custom
sentences and the existing HA Scheduler companion remain available in native
Assist mode. Existing companion polling can cache speech for new reminders;
until then, reminders use their configured local sound fallback. Already saved
alerts continue independently of voice workflow and cloud availability.

The controller stores only the tool key's hash in system configuration and
retains the connection credential in the private workflow record. API responses
omit it. Its bearer key permits only that speaker's MCP route. `EchoMuseStop`
is available while Pipecat is active; schedule changes are rejected when local
scheduling is disabled. Removing the satellite removes its key. The adapter
requires satellite authentication before
accepting session tool URLs or credentials. Keep that connection on a trusted
network or use TLS; headers contain credentials and the scheduler HTTP URL uses
the same trusted-LAN assumption as the existing companion API.

### How Home Assistant devices reach the live model

Pipecat connects to HA's MCP server using the Supervisor token under the add-on,
or a long-lived HA token for a standalone installation. Enable the HA MCP
Server integration and **expose the intended entities to Assist**. Pipecat
fetches the MCP tool schema, registers callbacks on the selected live LLM, and
includes the tools in the model's conversation context. The model can call
`homeassistant__GetLiveContext` to discover exposed device names, areas and
states, then call intent tools such as `intent__HassTurnOn` and
`intent__HassTurnOff`. HA performs exposure checks when executing those calls.

The endpoint's `client_id` identifies the satellite/session and conversation
memory. EchoMuse supplies the session device ID when entered, otherwise its
satellite ID. This is not an HA permission boundary. The area field is sent as
`X-EchoMuse-Area` metadata for custom servers; upstream Pipecat Home Assistant
does not consume that header or automatically limit MCP tools by room. Use
HA's exposure settings and the Pipecat pipeline's MCP tool allowlist to control
access. [Live validation](voice-workflow-validation.md) verifies that an exposed
light is discoverable and controllable while a hidden light remains inaccessible.

### Weather, forecasts and other custom attributes

`GetLiveContext` passes the model each exposed entity's name, domain, state and
only these attributes: `temperature`, `current_temperature`, `temperature_unit`,
`humidity`, `unit_of_measurement`, `device_class`, `brightness`,
`current_position`, `percentage`, `volume_level` and the media title, artist and
album. Anything else on an entity, such as an `hourly_forecast` or
`daily_forecast` attribute on a template sensor, is not shown to the model even
when the entity is exposed, and the model usually looks in the `weather` domain,
which a sensor is not in (HA answers `No exposed entities found in domain(s):
weather`). The model then says it cannot get the weather.

To give it weather or any other attribute text, expose a **script** to Assist.
An exposed script becomes a tool (`script__<name>`) and can return data:

```yaml
script:
  get_weather:
    alias: Get weather
    description: >-
      Use this for ANY question about the weather: current conditions,
      temperature, humidity, rain, or the hourly or daily forecast. Returns all
      of it.
    sequence:
      - variables:
          result:
            condition: "{{ states('sensor.weather_forecast_assistant') }}"
            temperature: "{{ state_attr('sensor.weather_forecast_assistant', 'temperature') }}"
            humidity: "{{ state_attr('sensor.weather_forecast_assistant', 'humidity') }}"
            hourly_forecast: "{{ state_attr('sensor.weather_forecast_assistant', 'hourly_forecast') }}"
            daily_forecast: "{{ state_attr('sensor.weather_forecast_assistant', 'daily_forecast') }}"
      - stop: done
        response_variable: result
```

Then expose `script.get_weather` under Settings, Voice assistants, Expose. The
description is what makes the model choose it; keep the "ANY question about the
weather" wording. With a real `weather` entity exposed the model can answer
current conditions from `GetLiveContext` alone, but not a forecast.

## Generic Pipecat servers

Other endpoint paths default to Pipecat's protobuf WebSocket transport, with
`ProtobufFrameSerializer`, 16 kHz mono input, and raw PCM output
(`add_wav_header=False`). 48 kHz mono output avoids resampling; the bundled
bridge also accepts 8, 16, 22.05, 24 and 44.1 kHz, mono or stereo, resampled as
one continuous signal per reply so the message size the server chooses cannot
be heard. Configure
server-side VAD/interruption handling. Protobuf `InterruptionFrame` flushes the
satellite's buffered speech. For a reverse proxy that changes the raw-PCM
endpoint path, the API accepts `transport: "va_pipecat"`; `"protobuf"` and
`"auto"` are also supported.

A `SingleClientWebsocketServerTransport` endpoint serves one satellite. Switch
to Home Assistant before changing an active connection's settings so that
server can accept the verification client. Use per-session FastAPI transports
for multiple satellites. A WebRTC signaling URL or the direct Gemini Live URL
cannot be used as the controller's audio endpoint.

Generic servers receive the Server API key as a Bearer header, the optional
provider key as `X-EchoMuse-Google-Api-Key`, and satellite/area/device metadata
in `X-EchoMuse-Device`, `X-EchoMuse-Area`, and `X-EchoMuse-Target-Device`. These
custom headers require a server implementation that reads them; they are not
built-in Pipecat configuration. Metadata does not enforce entity permissions.

## Runtime and storage

The bridge ships with the controller's existing `aiohttp`, `websockets`,
`protobuf`, `numpy` and `scipy` requirements. No optional bridge downloads or
package installation privileges are needed at activation. Both the add-on and
standalone image contain the same bridge. The remote Pipecat server and its
model dependencies are managed on that host. Existing satellite speaker
priming still adds latency; this feature does not replace the audio driver.

The setup test verifies connectivity, accepted satellite credentials and, for
the raw-PCM endpoint, the audio contract. It does not run a cloud model
conversation. Provider errors after the server's hello appear as an
unavailable/reconnecting session.

Settings live in the controller-only `voice_workflows` table (schema v32),
keyed by satellite ID and deleted with the satellite. They are independent of
fleet config and are never sent to firmware or included in support config
exports. API reads omit stored keys. Protect database backups; use `wss://` to
encrypt credentials and audio on untrusted networks.

`GET /api/devices/{id}/voice-workflow` returns redacted configuration, resolved
transport, dependency readiness and audio status. Authenticated users can read
it; `POST` requires admin access. POST merges `mode` (`home_assistant` or
`pipecat`), `endpoint`, `api_key`, `google_api_key`, `area`, `target_device`, and
optional `transport`. Omitted keys are retained; an explicit empty key clears
it locally. Selecting native mode never requires a running Pipecat server.
