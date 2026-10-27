"""Per-authenticated-session EchoMuse options; no global Pipecat config edits."""
import asyncio
import re
from urllib.parse import urlsplit

PROFILES = {
    # Alarm-stop failure 2026-10-22: every frame stayed below 0.7 (peaks
    # 0.59/0.69), so the live service never received a user-speech start.
    # Use Silero's default confidence for the sensitive profile; retain the
    # duration and volume gates so an isolated peak is still not speech.
    "responsive": {"confidence": 0.5, "start_secs": 0.12, "min_volume": 0.02},
    "balanced": {"confidence": 0.8, "start_secs": 0.30, "min_volume": 0.04},
    "low": {"confidence": 0.85, "start_secs": 0.45, "min_volume": 0.06},
}


def composed_instructions(settings, language):
    """Compact session policy; keep the realtime provider's existing prompt."""
    rules = [
        "EchoMuse voice rules: Confirm completed actions in a few words, then stop; no offers or optional follow-up questions. "
        "Only for a required missing detail, call EchoMuseRequestContinuation before asking one short question.",
        f"Reply in {language or 'en'} unless the user clearly speaks another language. "
        "Do not switch for accents, short speech, foreign names/titles or tool results.",
        (f"Speaker area: {settings['area']}." if settings.get('area') else
         "Use the speaker's current HA area.") + " Explicit room/device targets take precedence.",
    ]
    scheduler = settings.get('scheduler') or {}
    if scheduler:
        rules.append(
            "Bare stop/pause: call EchoMuseStop once. It stops one item: a ringing alert, else this speaker's music, else a running timer. "
            "Report only stopped and errors; never change left_running. A second user stop handles the next item. "
            "Only if stopped is empty with no errors, inspect HA context and pause playing players in this speaker's area "
            "and cancel only an active HA timer there. Never cancel future alarms/reminders.")
        rules.append(
            "Untargeted volume: use EchoMuseVolume for this speaker only. Use HA volume tools only for an explicit other device/room.")
    else:
        rules.append(
            "Bare stop/pause: inspect HA context; pause only playing players in this speaker's area and cancel only an active HA timer there. "
            "Never cancel future alarms/reminders. Untargeted volume changes only this speaker's player, never all room players.")
    if scheduler.get('schedules'):
        rules.append(
            "Use EchoMuse scheduler tools for timers/alarms/reminders; confirm saves only after device acknowledgment. "
            "Bare snooze: inspect EchoMuseList; snooze only the ringing alert.")
    rules.extend([
        timezone_rule(settings),
        "Bare play/resume/continue without a title: unpause this speaker's own player; never search for generic music or call play_media. "
        "For specific music, call play_media directly with plain title/artist. Success means playback started: no search first, "
        "verification or repeat call. Never invent Spotify URIs/playlist IDs; reuse only search-returned IDs.",
        "Use exact HA area names, never underscore substitutions. On an unknown-area error, inspect GetLiveContext and retry "
        "the real area name instead of asking the user. Maximum two GetLiveContext calls per turn; then answer from results or explain failure.",
        "Volume numbers are percentages: volume 10 means volume_level 10, never multiply by ten.",
    ])
    return "\n" + "\n".join(rules)


def timezone_rule(settings):
    """The household's zone by NAME, never a time or an offset.

    A Gemini Live session resumes for hours, so a time written into its
    instructions goes stale, across a clock change included. A zone name does
    not, and with it known the controller applies the offset itself.
    """
    zone = settings.get("timezone")
    if not zone:
        return "Ask if the timezone for a calendar alarm/reminder is unknown."
    return f"Household time zone: {zone}. Give alarm/reminder times as the local clock time, without an offset."


def options(headers, authenticated):
    # A public/no-secret satellite endpoint must not accept MCP credentials or
    # server URLs from arbitrary clients. Existing public clients still work.
    if not authenticated or not (headers.get("x-echomuse-barge-in") or headers.get("x-echomuse-scheduler-url")):
        return {}
    profile = headers.get("x-echomuse-barge-in", "balanced")
    result = {"barge_in": profile if profile in PROFILES else "balanced"}
    result["interrupt_by"] = headers.get("x-echomuse-interrupt-by", "voice")
    satellite_mac = headers.get("x-echomuse-satellite-mac", "").strip()
    if satellite_mac:
        result["satellite_mac"] = satellite_mac
    timezone = headers.get("x-echomuse-timezone", "").strip()
    if timezone:
        result["timezone"] = timezone
    url = headers.get("x-echomuse-scheduler-url", "")
    token = headers.get("x-echomuse-scheduler-token", "")
    if url and token:
        parts = urlsplit(url)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username or parts.password or parts.query or parts.fragment
                or not parts.path.endswith("/voice-workflow/mcp")
                or not token.startswith("emvoice.")):
            raise ValueError("Invalid EchoMuse scheduler configuration")
        result["scheduler"] = {
            "id": "echomuse", "name": "EchoMuse controls for this speaker",
            "url": url, "token": token, "prefer_unprefixed": True,
            "schedules": headers.get("x-echomuse-local-schedules", "false").lower() == "true",
        }
    area = headers.get("x-echomuse-area", "").strip()
    if area:
        result["area"] = area[:160]
    return result


def vad_analyzer(settings, fallback):
    profile = settings.get("barge_in")
    if profile not in PROFILES:
        return fallback()
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams
    return _logging_vad(SileroVADAnalyzer)(params=VADParams(**PROFILES[profile], stop_secs=0.3))


def _logging_vad(base):
    """Silero VAD that says what it heard, for a turn that logs no user start.

    Field log 2026-10-19: an alarm wake produced eight seconds of ingress
    windows and not one `User started speaking`, so nothing could tell a
    detector that heard no speech from one that heard it under its bar, or one
    that began the session already "speaking". This logs the state on every
    change and a one-line summary about once a second of audio: peak
    confidence, frames at or over the bar, peak volume. It costs two compares
    per VAD frame in the executor thread that already computes them, and a
    log call per second and per state change, so the audio path is unchanged.
    """
    try:
        from loguru import logger
    except ImportError:  # a test environment without Pipecat's own dependencies
        return base

    class LoggingVAD(base):
        _frames = 0
        _peak_conf = 0.0
        _peak_vol = 0.0
        _over = 0
        _run_over = 0
        _longest_run = 0
        _seen_state = None

        def voice_confidence(self, buffer):
            confidence = super().voice_confidence(buffer)
            # Silero returns a numpy array; track a float and hand back the original.
            seen = float(getattr(confidence, "item", lambda: confidence)())
            self._frames += 1
            self._peak_conf = max(self._peak_conf, seen)
            if seen >= self._params.confidence:
                self._over += 1
                self._run_over += 1
                self._longest_run = max(self._longest_run, self._run_over)
            else:
                self._run_over = 0
            return confidence

        def _run_analyzer(self, buffer):
            before = self._frames
            state = super()._run_analyzer(buffer)
            if before != self._frames:
                self._peak_vol = max(self._peak_vol, self._prev_volume)
            if state != self._seen_state:
                logger.debug("VAD state {} -> {} (starting={} stopping={})", getattr(self._seen_state, "name", None), state.name,
                             self._vad_starting_count, self._vad_stopping_count)
                self._seen_state = state
            if self._frames >= 31:
                logger.debug("VAD window: state={} peak_confidence={:.2f} frames_over_bar={}/{} bar={} peak_volume={:.3f} min_volume={} longest_confidence_run_ms={:.0f} start_ms={:.0f}",
                             state.name, self._peak_conf, self._over, self._frames, self._params.confidence,
                             self._peak_vol, self._params.min_volume,
                             self._longest_run * self.num_frames_required() / self.sample_rate * 1000,
                             self._params.start_secs * 1000)
                self._frames = self._over = 0
                self._peak_conf = self._peak_vol = 0.0
                self._longest_run = self._run_over
            return state

    return LoggingVAD


def composed_turn_start_strategies(settings):
    """VAD delimits speech; explicit wake/interrupt messages own wake barge-in."""
    from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy

    voice_interruptions = settings.get("interrupt_by") != "wake"
    # The controller already enters listening on wake. A VAD-generated user
    # start would also serialize a listening phase and reopen its closed mic.
    return [VADUserTurnStartStrategy(
        enable_interruptions=voice_interruptions,
        enable_user_speaking_frames=voice_interruptions,
    )]


# Cartesia closes a streaming socket after five minutes without a message and
# has no keepalive. Pipecat reconnects when it notices, but text sent in the
# same moment goes to a socket that is closing and is lost: the reply was
# generated, the speech never arrived, and the error that followed made the
# controller drop the connection (2026-10-19, "what's the indoor temperature").
CARTESIA_IDLE_REFRESH_S = 240.0


def cartesia_idle_refresh(base):
    """A Cartesia TTS service that renews its own socket before Cartesia ends it.

    After CARTESIA_IDLE_REFRESH_S without a request the socket is closed from
    our side, when nothing can be in flight, and Pipecat's own receive loop
    reconnects as it already does for the server's close. A request arriving
    in the few hundred milliseconds of that reconnect is handled by Pipecat's
    existing "socket closed, connect first" check in run_tts. Nothing is added
    to a request that finds a live socket, so it costs no latency.
    """
    import asyncio
    from loguru import logger

    class IdleRefreshCartesia(base):
        idle_refresh_s = CARTESIA_IDLE_REFRESH_S
        _em_active = 0.0
        _em_refresher = None

        def _em_touch(self):
            self._em_active = asyncio.get_running_loop().time()

        async def _connect_websocket(self):
            await super()._connect_websocket()
            self._em_touch()
            if self._em_refresher is None or self._em_refresher.done():
                self._em_refresher = self.create_task(self._em_refresh_idle(), "echomuse_cartesia_refresh")

        async def run_tts(self, text, context_id):
            self._em_touch()
            async for frame in super().run_tts(text, context_id):
                yield frame

        async def _em_refresh_idle(self):
            loop = asyncio.get_running_loop()
            while True:
                await asyncio.sleep(max(min(1.0, self.idle_refresh_s / 2),
                                        self.idle_refresh_s - (loop.time() - self._em_active)))
                if loop.time() - self._em_active < self.idle_refresh_s:
                    continue
                socket = getattr(self, "_websocket", None)
                if socket is not None and getattr(getattr(socket, "state", None), "name", "") == "OPEN":
                    logger.debug("Cartesia socket idle for {:.0f}s; renewing it before Cartesia ends it",
                                 self.idle_refresh_s)
                    self._em_touch()           # the reconnect restarts the clock
                    await socket.close()

    return IdleRefreshCartesia


_PREAMBLE_ROLES = ("system", "developer")


def trim_messages(messages, max_messages):
    """The conversation cut to its newest `max_messages`, starting at something a model accepts.

    A tool call and its result are separate messages and must stay together:
    a result with no call before it is refused by OpenAI-style providers, and
    a plain "last N" cut can start exactly there. So the cut moves forward to
    the next user message, which means "up to" N, never more. System and
    developer messages are kept wherever they are. Entries that are not dicts
    (Gemini's thought signatures ride beside the messages they belong to) are
    not counted and stay with what follows them. 0 keeps everything.
    """
    items = list(messages)
    if max_messages <= 0:
        return items
    body = [i for i, m in enumerate(items)
            if isinstance(m, dict) and m.get("role") not in _PREAMBLE_ROLES]
    if len(body) <= max_messages:
        return items
    start = body[-max_messages]
    users = [i for i in body if items[i].get("role") == "user"]
    later = [i for i in users if i >= start]
    if later:
        start = later[0]
    elif users:
        start = users[-1]           # the newest user message, whole turn after it
    else:
        start = body[-1]
    return [m for i, m in enumerate(items)
            if i >= start or (isinstance(m, dict) and m.get("role") in _PREAMBLE_ROLES)]


def mcp_options(settings, servers, allowlist):
    result = list(servers)
    allowed = list(allowlist)
    if scheduler := settings.get("scheduler"):
        result.append(scheduler)
        if allowed:
            allowed.extend(["EchoMuseStop", "EchoMuseVolume"])
            if scheduler.get("schedules"):
                allowed.extend(["EchoMuseCreate", "EchoMuseList", "EchoMuseAction"])
    return result, allowed


CONTINUATION_RESULT = (
    "The user's microphone will reopen briefly after your response. Ask only "
    "the required clarification, then finish that turn without another question."
)


async def answer_continuation(protocol, params, run_llm_off):
    """Record the request and hand the model its result without waiting for the bot to stop.

    Gemini 3 Live speaks and calls a tool in the same turn, and its tools block:
    the turn waits for the result. Pipecat holds a result back while the bot is
    speaking and pushes it when the bot stops, so a call made mid-reply stalled
    the audio for the length of the rest of the reply (3.0s of silence in the
    field) and the late result found a turn that had already ended, leaving it
    open until the next wake. The result is therefore sent to the model at once,
    and the aggregator is told not to send it a second time.

    `llm._tool_result` and `llm._completed_tool_calls` are private to Pipecat's
    Gemini Live service; a service without them (another provider) keeps the
    normal path, which is correct there because it does not block on the result.
    """
    protocol.continuation_requested = True
    llm = params.llm
    send = getattr(llm, "_tool_result", None)
    completed = getattr(llm, "_completed_tool_calls", None)
    if callable(send) and isinstance(completed, set):
        await send(params.tool_call_id, params.function_name, {"value": CONTINUATION_RESULT})
        completed.add(params.tool_call_id)
        await params.result_callback(CONTINUATION_RESULT, properties=run_llm_off())
        return
    await params.result_callback(CONTINUATION_RESULT)


def continuation_schema(protocol):
    """Build a per-satellite tool that explicitly opens one clarification turn."""
    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.frames.frames import FunctionCallResultProperties

    async def handler(params):
        await answer_continuation(protocol, params, lambda: FunctionCallResultProperties(run_llm=False))

    return FunctionSchema(
        name="EchoMuseRequestContinuation",
        description=(
            "Request one short user reply when you cannot complete the current request "
            "without a required missing detail. Call this before asking the clarification. "
            "Do not call it for optional follow-up, offers, or conversational questions."
        ),
        properties={},
        required=[],
        handler=handler,
    )


def mark_context_results_sent(service):
    """Stop Pipecat re-sending every old tool result after a reconnect.

    Field log 2026-10-22: Gemini ends the session every ~2.5 minutes and
    Pipecat's `_disconnect` empties `_completed_tool_calls`, but the context
    still holds every tool result of the conversation, and a new session is
    restored or seeded with them as history. After "Stop", the next result
    re-sent 13 historical results as live tool responses alongside the stop
    result. This was followed by 68 unrequested calls (including timer
    creation and vacuum scripts) in the supplied log. Historical results
    must not trigger fresh inference. Mark them sent before the connection
    starts receiving messages; leave IN_PROGRESS calls eligible to deliver
    their result when they actually complete.
    """
    completed = getattr(service, "_completed_tool_calls", None)
    context = getattr(service, "_context", None)
    if not isinstance(completed, set) or context is None:
        return
    for message in context.get_messages():
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        if message.get("content") == "IN_PROGRESS":
            continue
        if message.get("tool_call_id"):
            completed.add(message["tool_call_id"])


async def keep_connection(service, run, config):
    """Run Pipecat's Gemini Live connection task, and start over if it dies at connect.

    Field log 2026-10-18: every ~2.5 minutes Gemini ends the session and Pipecat
    reconnects with the saved resumption handle. At 06:48:40 the handle was
    refused ("1008 BidiGenerateContent session expired", 0.8s after connecting)
    and the satellite went deaf for five hours: an alarm rang, "stop" got no
    answer, and the Echo had to be pressed. The refusal comes from the
    `async with connect(...)` line of the connection task, outside the receive
    loop's try, so it never reaches `_handle_connection_error`: the task dies,
    `_session` stays empty, and every later audio frame is dropped without a
    word. Here the failure is counted, the dead handle is dropped (it will be
    refused again; without one Pipecat opens a fresh session and re-seeds the
    conversation from its context) and a reconnect is started from a new task,
    so a task is not cancelling itself. After three failures in a row Pipecat's
    own limit pushes the fatal error, which the controller answers by
    rebuilding the connection.
    """
    mark_context_results_sent(service)
    try:
        await run(config)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        if getattr(service, "_disconnecting", False):
            raise
        service._session_resumption_handle = None
        if await service._handle_connection_error(error):
            service.create_task(service._reconnect(), "echomuse_gemini_reconnect")


AREA_MISSING = "The area does not exist"


def area_spellings(area):
    """Other ways to write the same area name, most likely first.

    Home Assistant matches an area by its name, ignoring case ("Living Room"),
    or by its exact id ("living_room"). A model that has seen both writes a
    blend such as "Living_Room", which matches neither.
    """
    words = [w for w in re.split(r"[\s_\-]+", area.strip()) if w]
    if not words:
        return []
    candidates = [" ".join(words), " ".join(words).title(), "_".join(words).lower(), " ".join(words).lower()]
    seen, result = {area}, []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result


async def call_with_area_fallback(call, arguments):
    """Run a tool call; if Home Assistant says the area does not exist, try its other spellings.

    `call(arguments)` returns the tool's text. The spelling that works is used
    and the model never sees the failure; if none does, the original answer is
    returned so a genuinely unknown area is still reported.
    """
    text = await call(arguments)
    area = arguments.get("area") if isinstance(arguments, dict) else None
    if not isinstance(area, str) or AREA_MISSING not in text:
        return text
    for spelling in area_spellings(area):
        retry = await call({**arguments, "area": spelling})
        if AREA_MISSING not in retry:
            return retry
    return text


# Home Assistant namespaces every MCP tool with its LLM API id (`assist__light__HassLightSet`)
# once its MCP server serves more than one API, and drops the prefix again when it serves one.
# Which of the two it is changes while the add-on runs (an integration registering its own API
# after a restart or reload), and a model holding the other spelling is told the tool does not
# exist, for every tool, not one.
TOOL_NOT_FOUND = re.compile(r'(?:Tool "?[^"\n]+"? not found|Unknown tool: ?\S+)')
NAMESPACE_SEP = "__"


def tool_not_found(text):
    """True for an MCP server's answer to a tool name it does not have.

    That answer means nothing ran, so asking again under another name cannot do a thing twice.
    """
    return isinstance(text, str) and len(text) < 300 and bool(TOOL_NOT_FOUND.search(text))


def renamed_tool(name, current):
    """The one name in `current` that is `name` with one namespace segment added or removed, else None."""
    if name in current:
        return name
    wider = [n for n in current if n.partition(NAMESPACE_SEP)[2] == name]
    if len(wider) == 1:
        return wider[0]
    narrower = name.partition(NAMESPACE_SEP)[2]
    return narrower if narrower in current else None


def known_rename(bridge, name):
    return getattr(bridge, "_em_renamed", {}).get(name, name)


async def recover_renamed_tool(bridge, requested, tried, text, retry):
    """After a not-found answer, relist the server's tools and retry once under the current name.

    `requested` is the name the model used, `tried` what was sent. A rename found is remembered
    on the bridge so later calls go straight to it, and the relist replaces the cached tool list
    for every other session as well. Anything but a clean rename returns the original answer.
    """
    if not tool_not_found(text):
        return text
    try:
        schema = await bridge.tools_schema(refresh=True)
        current = renamed_tool(requested, {tool.name for tool in schema.standard_tools})
    except Exception:
        return text
    if current is None or current == tried:
        return text
    answer = await retry(current)
    if tool_not_found(answer):
        return text
    if not hasattr(bridge, "_em_renamed"):
        bridge._em_renamed = {}
    bridge._em_renamed[requested] = current
    return answer


def renamed_route(routes, name):
    """A pooled bridge's route for a tool the model knows by an older spelling, else None."""
    current = renamed_tool(name, set(routes))
    return routes[current] if current is not None else None
