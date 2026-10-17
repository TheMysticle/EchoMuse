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
