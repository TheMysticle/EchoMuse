#!/usr/bin/env python3
"""Package a pinned upstream Pipecat add-on with the EchoMuse session adapter.

Usage: python tools/pipecat_echomuse/package.py --source /path/to/upstream
The adapter is kept here; upstream source is not vendored into the repository.
"""
import argparse
from pathlib import Path
import re
import shutil
import subprocess

PIN = "a538d1d1bacc8609061719f0b05c1622147ccb4b"
ROOT = Path(__file__).resolve().parents[2]


def adapt(text):
    def replace(old, new):
        nonlocal text
        if text.count(old) != 1:
            raise ValueError("Upstream adapter anchor changed: " + old[:70])
        text = text.replace(old, new)
    replace('from __future__ import annotations', 'from __future__ import annotations\n\nfrom app import echomuse\nfrom app import fast_path\nfrom app import mcp_bridge\nfrom app import tool_scope')
    replace('    await websocket.send_text(_va_pipecat_protocol(config).hello())', '''    hello = json.loads(_va_pipecat_protocol(config).hello())
    hello["echomuse"] = {"scheduler": bool(session_options.get("scheduler", {}).get("schedules")), "stop": bool(session_options.get("scheduler")), "barge_in": bool(session_options.get("barge_in")), "device_endpoint": _runtime_mode(flow) == "composed"}
    await websocket.send_text(json.dumps(hello))''')
    replace('    session_id = str(uuid.uuid4())\n    await websocket.accept()', '''    session_options = echomuse.options(websocket.headers, authenticated=bool(secret))
    session_id = str(uuid.uuid4())
    await websocket.accept()''')
    replace('            "transport": "va-pipecat",', '            "transport": "va-pipecat",\n            "echomuse": session_options,')
    replace('        async def _handle_connection_error(self, error: Exception) -> bool:\n            connection_started', '''        async def _connection_task_handler(self, config):
            await echomuse.keep_connection(self, super()._connection_task_handler, config)

        async def _handle_connection_error(self, error: Exception) -> bool:
            connection_started''')
    replace('    mcp_servers = config.enabled_mcp_servers(mcp_token) if flow.mcp_enabled else []\n    if mcp_servers:\n        bridge = CombinedMCPBridge(mcp_servers, flow.mcp_tool_allowlist)', '''    mcp_servers = config.enabled_mcp_servers(mcp_token) if flow.mcp_enabled else []
    mcp_servers, session_allowlist = echomuse.mcp_options(session_body.get("echomuse", {}), mcp_servers, flow.mcp_tool_allowlist)
    if mcp_servers:
        bridge = CombinedMCPBridge(mcp_servers, session_allowlist)''')
    replace('    session_body = _runner_body(runner_args)\n    is_esphome_satellite', '''    session_body = _runner_body(runner_args)
    if session_body.get("echomuse"):
        flow = flow.model_copy(deep=True)
        area = session_body["echomuse"].get("area")
        area_hint = f" The speaker is in the {area} area; treat that as the room for bare media stop requests." if area else " Use the speaker's current Home Assistant area for bare media stop requests."
        lang_hint = f" Always reply in the language with code {flow.language or 'en'}, the satellite's configured language, unless the user clearly speaks a different language to you. Accented or very short speech is not a reason to switch, and names or titles in another language in tool results or the home setup do not change your reply language."
        if _runtime_mode(flow) == "composed":
            flow.instructions += echomuse.composed_instructions(session_body["echomuse"], flow.language)
        else:
            flow.instructions += "\\nFor EchoMuse: keep replies short. After you do what was asked, confirm in a few words and stop (for example: Light off.). Never end a reply with an offer or a question such as: Can I help with anything else? Is there anything else? Let me know if you need anything. Ask a question only when a required detail is missing, and then call EchoMuseRequestContinuation before asking one short clarification; never use it for optional follow-up. Use EchoMuse scheduler tools for timers, alarms and reminders; never claim a save without device acknowledgment. For a bare stop — the word stop alone, or stop or pause said of the music with no device or room named — call EchoMuseStop once. It stops one thing: what is ringing first, otherwise this speaker's music, otherwise a running timer, and reports `stopped` and `left_running`. Report only what `stopped` lists, explain any reported errors, and never stop, pause or cancel anything listed in `left_running`: that is deliberate, and a second stop does the next thing. Only when `stopped` is empty and there are no errors, use homeassistant__GetLiveContext or equivalent to find a playing media_player in the speaker's area and call intent__HassMediaPause for only those active room players; if a Home Assistant timer in that area is active and a timer-cancel tool is available, cancel it. Never cancel future alarms or reminders. For a bare snooze, inspect EchoMuseList and snooze only the active ringing alert. Explicit room or device targets take precedence over this bare-stop behavior. For a bare play, resume or continue with no title, resume this speaker's own media player (the Home Assistant media unpause tool) and never call play_media. When asked for specific music, pass the title or artist as plain text to play_media; never write a Spotify URI or playlist ID yourself, only reuse one that a search returned. play_media with a title starts playback at once and its success means the music is playing, so never search first, verify afterwards or call it again for the same request. Never search for a generic word such as music to find something to resume; a bare play or resume is the media unpause tool. Use an area name exactly as GetLiveContext lists it, for example 'Living Room': never join its words with underscores or change its capitalisation. If a tool says an area does not exist, do not ask the user which area it is in; call GetLiveContext ONCE to read the real area names and try again. Never call GetLiveContext more than twice in one turn total: if the second call still does not resolve the request, answer from what it returned, or tell the user what you could not find, rather than calling it again. A spoken volume number is a percentage whether or not the user says percent: 'volume 10' and 'set the volume to 10' mean volume_level 10, which is ten percent. Never read it as a number out of ten and never multiply it; pass the number you heard. For a bare volume change with no device or room named — 'volume 20', 'turn it up', 'turn the volume down' — call EchoMuseVolume with that percentage; it changes only this speaker. Never use a Home Assistant volume tool for a bare request like this: given only an area it can change every matching media player there, a TV included. Use a Home Assistant volume tool only when the user names a different device or room. Ask about an unknown timezone before creating a calendar alarm or reminder. Do not close any reply by offering more help." + area_hint + lang_hint
    is_esphome_satellite''')
    replace('    local_tool_schemas = [schema for schema in [_web_search_tool_schema(config, flow)] if schema]\n    tools_schema = _merge_tools_schema(mcp_tools_schema, local_tool_schemas)\n    context_for_memory = None', '''    local_tool_schemas = [schema for schema in [_web_search_tool_schema(config, flow)] if schema]
    if session_body.get("echomuse") and session_body["transport"] == "va-pipecat":
        params = getattr(transport.input(), "_params", None)
        serializer = getattr(params, "serializer", None)
        protocol = getattr(serializer, "protocol", None)
        if protocol is not None:
            local_tool_schemas.append(echomuse.continuation_schema(protocol))
    tools_schema = _merge_tools_schema(mcp_tools_schema, local_tool_schemas)
    context_for_memory = None''')
    replace('local_turn_detection=bool(_enabled_step(flow, "vad")),', 'local_turn_detection=bool(_enabled_step(flow, "vad") or session_body.get("echomuse", {}).get("barge_in")),')

    # --- upstream bug: _step_model_for's "stt" branch never falls back to
    # integration.default_model, unlike every other branch beside it (tts
    # does). The UI's own stepDefaults() in main.jsx already assumes this
    # fallback exists (default_stt_model || default_model || fallback) and
    # shows the right value as a result, which is exactly why a correctly
    # configured Deepgram/Soniox/Speechmatics integration (nova-3 saved in
    # its one "STT model" field, which writes default_model) still resolved
    # to an empty model at runtime -- "AsyncV1Client.connect() missing 1
    # required keyword-only argument: 'model'", reconnecting forever. ---
    replace(
        'if capability == "stt":\n        return (integration.default_stt_model or fallback).strip()',
        'if capability == "stt":\n        return (integration.default_stt_model or integration.default_model or fallback).strip()',
    )
    replace('if provider_kind == "gemini" and _enabled_step(flow, "vad"):', 'if provider_kind == "gemini" and (_enabled_step(flow, "vad") or session_body.get("echomuse", {}).get("barge_in")):')
    replace('vad_analyzer=_composed_vad_analyzer(flow),', 'vad_analyzer=echomuse.vad_analyzer(session_body.get("echomuse", {}), lambda: _composed_vad_analyzer(flow)),')

    # --- local STT/TTS providers (sherpa-onnx, Piper/Wyoming) ---
    replace(
        '    clear_mcp_tools_cache,\n    list_mcp_call_history,\n)',
        '    clear_mcp_tools_cache,\n    close_all_pooled_bridges,\n    list_mcp_call_history,\n)',
    )
    replace(
        '    raise RuntimeError(f"STT provider {integration.kind} is not supported by composed runtime")',
        '''    if integration.kind == "sherpa_onnx":
        from app.sherpa_stt import SherpaOnnxSTTService

        host, _, port = (integration.base_url or integration.endpoint or "").partition(":")
        if not host or not port:
            raise RuntimeError("sherpa-onnx STT is missing a host:port endpoint")
        return SherpaOnnxSTTService(host=host, port=int(port))

    raise RuntimeError(f"STT provider {integration.kind} is not supported by composed runtime")''',
    )
    replace(
        '    raise RuntimeError(f"TTS provider {integration.kind} is not supported by composed runtime")',
        '''    if integration.kind == "piper_wyoming":
        from app.piper_wyoming_tts import PiperWyomingTTSService

        host, _, port = (integration.base_url or integration.endpoint or "").partition(":")
        if not host or not port:
            raise RuntimeError("Piper/Wyoming TTS is missing a host:port endpoint")
        return PiperWyomingTTSService(host=host, port=int(port))

    raise RuntimeError(f"TTS provider {integration.kind} is not supported by composed runtime")''',
    )

    # --- Groq as an LLM provider (OpenAI-compatible, pipecat ships the
    # service already -- https://api.groq.com/openai/v1 is its default). ---
    replace(
        '    raise RuntimeError(f"LLM provider {integration.kind} is not supported by composed runtime")',
        '''    if integration.kind == "groq":
        from pipecat.services.groq.llm import GroqLLMService

        if not integration.api_key:
            raise RuntimeError("Groq is missing api_key")
        settings_kwargs: dict[str, Any] = {
            "model": model or integration.default_model or "llama-3.3-70b-versatile",
            "system_instruction": _effective_instructions(flow),
        }
        if flow.max_output_tokens:
            settings_kwargs["max_tokens"] = flow.max_output_tokens
        return GroqLLMService(
            api_key=integration.api_key,
            base_url=integration.base_url or "https://api.groq.com/openai/v1",
            settings=GroqLLMService.Settings(**settings_kwargs),
        )

    raise RuntimeError(f"LLM provider {integration.kind} is not supported by composed runtime")''',
    )

    replace(
        '    raise RuntimeError(f"LLM provider {integration.kind} is not supported by composed runtime")',
        '''    if integration.kind == "cerebras":
        from pipecat.services.openai.llm import OpenAILLMService

        if not integration.api_key:
            raise RuntimeError("Cerebras is missing api_key")
        settings_kwargs = {
            "model": getattr(step, "model", "") or integration.default_model or "gpt-oss-120b",
            "system_instruction": _effective_instructions(flow),
        }
        if flow.max_output_tokens:
            settings_kwargs["max_tokens"] = flow.max_output_tokens
        return OpenAILLMService(
            api_key=integration.api_key,
            base_url=integration.base_url or "https://api.cerebras.ai/v1",
            settings=OpenAILLMService.Settings(**settings_kwargs),
        )

    raise RuntimeError(f"LLM provider {integration.kind} is not supported by composed runtime")''',
    )
    replace(
        '        if integration.kind in {"openai", "openai_cloud"} and (integration.api_key or config.openai_api_key):',
        '''        if integration.kind == "cerebras" and capability == "llm" and integration.api_key:
            headers = {"Authorization": f"Bearer {integration.api_key}"}
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(
                    (integration.base_url or "https://api.cerebras.ai/v1").rstrip("/") + "/models",
                    headers=headers,
                )
                response.raise_for_status()
            models = sorted({item["id"] for item in response.json().get("data", [])
                             if isinstance(item.get("id"), str) and item["id"]})
            if models:
                return {"ok": True, "models": [{"id": item, "label": item} for item in models]}

        if integration.kind in {"openai", "openai_cloud"} and (integration.api_key or config.openai_api_key):''',
    )

    # --- MCP bridge pooling: reconnecting both configured MCP servers from
    # scratch (HA's, the controller's own per-device scheduler endpoint)
    # measured 1-2s; a va_pipecat session is one bounded turn per
    # wake/button press, so that cost was paid every turn. Pool per
    # client_id instead, close only on addon shutdown. ---
    replace(
        '''    if mcp_servers:
        bridge = CombinedMCPBridge(mcp_servers, session_allowlist)
        try:
            await bridge.start()''',
        '''    if mcp_servers:
        try:
            try:
                bridge = await mcp_bridge.get_pooled_bridge(client_id, mcp_servers, session_allowlist)
            except Exception:
                # The pooled entry may be dead (server restarted, link dropped).
                # One retry against a fresh connection before giving up.
                await mcp_bridge.evict_pooled_bridge(client_id)
                bridge = await mcp_bridge.get_pooled_bridge(client_id, mcp_servers, session_allowlist)''',
    )
    replace(
        '''        except asyncio.CancelledError as err:
            with suppress(Exception):
                await bridge.close()
            bridge = None
            raise RuntimeError(f"MCP tools are enabled but startup was cancelled: {err}") from err
        except Exception as err:
            with suppress(Exception):
                await bridge.close()
            bridge = None
            raise RuntimeError(f"MCP tools are enabled but unavailable: {err}") from err''',
        '''        except asyncio.CancelledError as err:
            await mcp_bridge.evict_pooled_bridge(client_id)
            bridge = None
            raise RuntimeError(f"MCP tools are enabled but startup was cancelled: {err}") from err
        except Exception as err:
            await mcp_bridge.evict_pooled_bridge(client_id)
            bridge = None
            raise RuntimeError(f"MCP tools are enabled but unavailable: {err}") from err''',
    )
    replace(
        '''        if bridge:
            await bridge.close()
        if audio_debug:''',
        '''        # The MCP bridge is NOT closed here -- it stays pooled (keyed by
        # client_id) for the next turn. See mcp_bridge.get_pooled_bridge.
        if audio_debug:''',
    )
    replace(
        "async def _shutdown_esphome_provisioner() -> None:\n    await ESPHOME_PROVISIONER.stop()",
        "async def _shutdown_esphome_provisioner() -> None:\n    await ESPHOME_PROVISIONER.stop()\n    await close_all_pooled_bridges()",
    )

    # Satellite audio still contains room noise before its end marker. An
    # energy gate mistakes that noise for new speech, interrupting inference.
    # Keep speech classification and negotiate interruptions separately.
    replace(
        '''def _composed_vad_analyzer(flow: FlowConfig):
    """Return local VAD tuned for browser and ESP32 composed pipelines."""

    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams''',
        '''def _composed_vad_analyzer(flow: FlowConfig, is_esphome_satellite: bool = False):
    """Return local VAD tuned for browser, ESP32 and EchoMuse satellite composed pipelines."""

    from pipecat.audio.vad.vad_analyzer import VADParams

    from pipecat.audio.vad.silero import SileroVADAnalyzer''',
    )
    replace(
        'vad_processor = VADProcessor(vad_analyzer=_composed_vad_analyzer(flow)) if vad_step else None',
        'vad_processor = VADProcessor(vad_analyzer=_composed_vad_analyzer(flow, is_esphome_satellite)) if vad_step else None',
    )
    replace(
        '                    user_turn_strategies=UserTurnStrategies(\n                        stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.2)]',
        '''                    user_turn_strategies=UserTurnStrategies(
                        start=echomuse.composed_turn_start_strategies(session_body.get("echomuse", {})),
                        stop=[SpeechTimeoutUserTurnStopStrategy(
                            user_speech_timeout=getattr(stt, "extra_turn_silence_secs", 0.2))]''',
    )

    # Service settings already supply the full system instructions. Keeping
    # another developer copy doubles them on Groq/OpenAI-compatible adapters.
    # Flow graphs retain their existing role-message lifecycle.
    replace(
        '        context_messages = [{"role": "developer", "content": _effective_instructions(flow)}]',
        '        context_messages = [{"role": "developer", "content": _effective_instructions(flow)}] if _flow_enabled(flow) else []',
    )

    # --- fast-path intent matching: a transcript matching one of Home
    # Assistant's own built-in sentence templates goes straight to the
    # matching MCP intent tool, skipping the LLM (app/fast_path.py). ---
    replace(
        '''def _composed_vad_analyzer(flow: FlowConfig, is_esphome_satellite: bool = False):''',
        '''_FAST_PATH_STATES: dict[str, "fast_path.FastPathState"] = {}


def _fast_path_custom_sentence_dirs(config: RuntimeConfig) -> list[Path]:
    dirs = [Path("/homeassistant/custom_sentences")]  # HA's own files, see config.yaml's map
    dirs.extend(Path(d) for d in config.fast_path_custom_sentences_dirs if d.strip())
    dirs.append(Path(__file__).resolve().parent.parent / "custom_sentences")  # addon-authored
    return dirs


def _fast_path_state(config: RuntimeConfig, language: str) -> "fast_path.FastPathState":
    cached = _FAST_PATH_STATES.get(language)
    if cached is not None:
        return cached
    intents, report = fast_path.load_intents(language, _fast_path_custom_sentence_dirs(config))
    state = fast_path.FastPathState(intents=intents, report=report)
    _FAST_PATH_STATES[language] = state
    return state


async def _build_fast_path_step(
    config: RuntimeConfig,
    flow: FlowConfig,
    bridge: CombinedMCPBridge,
    mcp_tools_schema,
    session_options: dict | None = None,
    protocol=None,
    context=None,
) -> "fast_path.FastPathStep | None":
    """Build the fast-path step for one turn, refreshing live entity/area
    names when stale. Returns None if no fast-path-eligible tool is exposed
    (e.g. Home Assistant MCP is not connected)."""

    language = (flow.language or "en").split("-")[0]
    # Ask HA's actual local agent first, with its device/area context and
    # custom intent handlers. Explicit MCP allowlists retain their restriction.
    from app.local_commands import from_config

    commands = None if flow.mcp_tool_allowlist else from_config(
        config, language=flow.language or "en",
    )
    if commands is not None:
        await commands.resolve_device((session_options or {}).get("satellite_mac", ""))
        # A real state is still needed here: without one, _recognize_any has
        # nothing to match against, so an EchoMuseCreate/List/Action utterance
        # can never be told apart from a Hass* one, and every utterance -- the
        # exact "start a timer" case this fast-path exists for -- goes to HA's
        # agent unchanged. slot_lists must be populated too, even with empty
        # name/area lists: hassil raises for the WHOLE merged grammar (which
        # still contains HA's own {name}/{area}-requiring built-ins) when a
        # list it references is entirely absent, not merely empty.
        state = _fast_path_state(config, language)
        if state.slot_lists_stale():
            live_context_tool = next(
                (t.name for t in mcp_tools_schema.standard_tools if t.name.endswith("GetLiveContext")),
                None,
            )
            if live_context_tool:
                raw = await bridge.call_tool(live_context_tool, {})
                names, areas = fast_path.parse_live_context(raw)
                state.set_slot_lists(names, areas)
                logger.info("Fast-path: refreshed {} names / {} areas", len(names), len(areas))
        return fast_path.FastPathStep(
            state=state, language=language, bridge=bridge, tool_names={},
            local_commands=commands, protocol=protocol, context=context,
            timezone=(session_options or {}).get("timezone"),
        )
    tool_names = fast_path.tool_names_for_fast_path(
        [tool.name for tool in mcp_tools_schema.standard_tools]
    )
    if not tool_names and not any(tool.name.endswith("__script__get_weather")
                                 for tool in mcp_tools_schema.standard_tools):
        return None

    state = _fast_path_state(config, language)
    if state.slot_lists_stale():
        live_context_tool = next(
            (t.name for t in mcp_tools_schema.standard_tools if t.name.endswith("GetLiveContext")),
            None,
        )
        if live_context_tool:
            raw = await bridge.call_tool(live_context_tool, {})
            names, areas = fast_path.parse_live_context(raw)
            state.set_slot_lists(names, areas)
            logger.info("Fast-path: refreshed {} names / {} areas", len(names), len(areas))

    return fast_path.FastPathStep(
        state=state, language=language, bridge=bridge, tool_names=tool_names, context=context,
        timezone=(session_options or {}).get("timezone"),
    )


def _composed_vad_analyzer(flow: FlowConfig, is_esphome_satellite: bool = False):''',
    )
    replace(
        '''        if _flow_enabled(flow):
            initial_flow_node = _flow_node_configs(flow, bridge)''',
        '''        fast_path_step = fast_path.FastPathStep(
            state=None, language=flow.language or "en", bridge=None, tool_names={}, context=context,
        ) if is_esphome_satellite and not _flow_enabled(flow) else None
        if (
            config.fast_path_enabled
            and is_esphome_satellite
            and bridge is not None
            and mcp_tools_schema is not None
            and not _flow_enabled(flow)
        ):
            try:
                params = getattr(transport.input(), "_params", None)
                protocol = getattr(getattr(params, "serializer", None), "protocol", None)
                fast_path_step = await _build_fast_path_step(
                    config, flow, bridge, mcp_tools_schema,
                    session_body.get("echomuse", {}), protocol, context,
                ) or fast_path_step
            except Exception as err:
                logger.warning(
                    "Fast-path setup failed, composed pipeline will use the LLM for every turn: {}", err
                )

        if _flow_enabled(flow):
            initial_flow_node = _flow_node_configs(flow, bridge)''',
    )
    replace(
        '        processors.extend([stt, context_aggregator.user(), llm, tts])',
        '''        processors.append(stt)
        processors.append(context_aggregator.user())
        if fast_path_step:
            processors.append(fast_path_step)
        if not _flow_enabled(flow):
            processors.append(tool_scope.ToolScopeStep(context=context, language=flow.language or "en"))
        processors.extend([llm, tts])''',
    )
    replace(
        '@app.get("/api/assist/mcp/history")\nasync def api_mcp_history():\n    return list_mcp_call_history()',
        '''@app.get("/api/assist/mcp/history")
async def api_mcp_history():
    return list_mcp_call_history()


@app.get("/api/assist/fast_path/status")
async def api_fast_path_status():
    # Read-only: builds (and caches, same as a real turn would) the compiled
    # grammar for each configured flow's language and reports what it found.
    # Never on the voice-turn path -- only when this HTTP endpoint is polled.
    config = STORE.load()
    languages = sorted({(flow.language or "en").split("-")[0] for flow in config.flows})
    reports = []
    for language in languages:
        try:
            state = _fast_path_state(config, language)
        except Exception as err:
            reports.append({"language": language, "error": str(err)})
            continue
        reports.append(fast_path.report_to_dict(state.report) if state.report else {"language": language})
    from app.local_commands import from_config
    return {
        "enabled": config.fast_path_enabled,
        "home_assistant_agent_configured": from_config(config, language="en") is not None,
        "languages": reports,
    }''',
    )
    return text


def adapt_config(text):
    """Patches for app/config.py: new provider kinds and fast-path settings."""

    def replace(old, new):
        nonlocal text
        if text.count(old) != 1:
            raise ValueError("Upstream config.py anchor changed: " + old[:70])
        text = text.replace(old, new)

    replace(
        '    "speechmatics",\n    "gradium",\n    "openai_cloud",\n}\nCOMPOSED_LLM_PROVIDER_KINDS = {',
        '    "speechmatics",\n    "gradium",\n    "openai_cloud",\n    "sherpa_onnx",\n}\nCOMPOSED_LLM_PROVIDER_KINDS = {',
    )
    replace(
        '    "elevenlabs",\n    "openai_cloud",\n    "soniox",\n}\nCOMPOSED_STEP_PROVIDER_KINDS = {',
        '    "elevenlabs",\n    "openai_cloud",\n    "soniox",\n    "piper_wyoming",\n}\nCOMPOSED_STEP_PROVIDER_KINDS = {',
    )
    replace(
        '    "openai_compatible",\n    "ollama",\n}\nCOMPOSED_TTS_PROVIDER_KINDS = {',
        '    "openai_compatible",\n    "ollama",\n    "groq",\n    "cerebras",\n}\nCOMPOSED_TTS_PROVIDER_KINDS = {',
    )
    replace(
        '        "home_assistant_mcp",\n        "ha_mcp",\n        "mcp_server",\n    ]',
        '        "home_assistant_mcp",\n        "ha_mcp",\n        "mcp_server",\n        "sherpa_onnx",\n        "piper_wyoming",\n        "groq",\n        "cerebras",\n    ]',
    )
    replace(
        'DEFAULT_SPEECHMATICS_MODEL = "enhanced"',
        'DEFAULT_SPEECHMATICS_MODEL = "enhanced"\nDEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"\nDEFAULT_CEREBRAS_MODEL = "gpt-oss-120b"',
    )
    replace(
        '        IntegrationConfig(\n            id="ollama",\n            name="Ollama",\n            kind="ollama",\n            enabled=False,\n            base_url="http://localhost:11434/v1",\n            default_model="llama3.2",\n        ),',
        '''        IntegrationConfig(
            id="ollama",
            name="Ollama",
            kind="ollama",
            enabled=False,
            base_url="http://localhost:11434/v1",
            default_model="llama3.2",
        ),
        IntegrationConfig(
            id="groq",
            name="Groq",
            kind="groq",
            enabled=False,
            api_key=os.getenv("GROQ_API_KEY", ""),
            default_model=DEFAULT_GROQ_MODEL,
        ),
        IntegrationConfig(
            id="cerebras",
            name="Cerebras Cloud",
            kind="cerebras",
            enabled=False,
            api_key=os.getenv("CEREBRAS_API_KEY", ""),
            base_url="https://api.cerebras.ai/v1",
            default_model=DEFAULT_CEREBRAS_MODEL,
        ),''',
    )
    replace(
        '    mcp_tools_cache_enabled: bool = True\n    mcp_tools_cache_ttl_seconds: int = Field(default=300, ge=0, le=86400)\n    ai_image_provider_id: str = ""',
        '    mcp_tools_cache_enabled: bool = True\n    mcp_tools_cache_ttl_seconds: int = Field(default=300, ge=0, le=86400)\n    fast_path_enabled: bool = True\n    fast_path_custom_sentences_dirs: list[str] = Field(default_factory=list)\n    ai_image_provider_id: str = ""',
    )
    return text


def adapt_text_agent(text):
    """Cerebras also serves the add-on's HA Assist text conversation route."""
    replacements = [
        ('        "openai_compatible",\n        "ollama",',
         '        "openai_compatible",\n        "cerebras",\n        "ollama",'),
        ('    if provider_kind in {"gemini", "gemini_cloud"}:\n        api_key =',
         '    if provider_kind == "cerebras":\n        api_key = integration.api_key if integration else ""\n    elif provider_kind in {"gemini", "gemini_cloud"}:\n        api_key ='),
        ('            client = AsyncOpenAI(**client_kwargs)',
         '''            if provider_kind == "cerebras":
                client_kwargs["base_url"] = integration.base_url or "https://api.cerebras.ai/v1"
            client = AsyncOpenAI(**client_kwargs)'''),
        ('    step = flow.model_step()\n    candidates = [',
         '''    step = flow.model_step()
    if provider_kind == "cerebras":
        return (getattr(step, "model", "") if step else "") or (integration.default_model if integration else "") or "gpt-oss-120b"
    candidates = ['''),
    ]
    for old, new in replacements:
        if text.count(old) != 1:
            raise ValueError("Upstream text_agent.py anchor changed: " + old[:70])
        text = text.replace(old, new)
    return text


def adapt_mcp_bridge_pool(text):
    """Appends the per-device MCP bridge pool to app/mcp_bridge.py."""

    marker = "async def check_mcp("
    if text.count(marker) != 1:
        raise ValueError("Upstream mcp_bridge.py anchor changed: " + marker)
    pool_code = '''_BRIDGE_POOL: dict[str, tuple[str, "CombinedMCPBridge"]] = {}
_BRIDGE_POOL_LOCK = asyncio.Lock()


def _pool_spec_key(servers: Sequence[dict[str, Any]]) -> str:
    """A server list's identity for pool reuse: same servers + same tokens, reuse; otherwise reconnect."""
    parts = sorted(
        f"{spec.get('id', '')}|{spec.get('url', '')}|{spec.get('token', '')}|{spec.get('prefer_unprefixed')}"
        for spec in servers
    )
    return "\\x1f".join(parts)


async def get_pooled_bridge(
    pool_id: str,
    servers: Sequence[dict[str, Any]],
    tool_allowlist: Sequence[str] | None = None,
) -> "CombinedMCPBridge":
    """Return a connected MCP bridge for `pool_id`, reusing a live one when its server set is unchanged.

    A fresh CombinedMCPBridge connects to every configured MCP server (Home
    Assistant's and the controller's own per-device scheduler endpoint) from
    scratch -- measured at 1-2s on this fleet. A va_pipecat session is one
    bounded turn, opened and closed per wake/button press, so reconnecting on
    every turn paid that cost on every turn. Keyed by pool_id (one per
    device/client), not globally: each device's scheduler token differs, so
    pooling across devices would hand one device's tools to another.
    """
    key = _pool_spec_key(servers)
    async with _BRIDGE_POOL_LOCK:
        cached = _BRIDGE_POOL.get(pool_id)
        if cached and cached[0] == key:
            return cached[1]
        stale = _BRIDGE_POOL.pop(pool_id, None)
        bridge = CombinedMCPBridge(servers, tool_allowlist)
        await bridge.start()
        _BRIDGE_POOL[pool_id] = (key, bridge)
    if stale:
        with suppress(Exception):
            await stale[1].close()
    return bridge


async def evict_pooled_bridge(pool_id: str) -> None:
    """Drop and close a pooled bridge -- call this when a call through it fails mid-turn."""
    async with _BRIDGE_POOL_LOCK:
        cached = _BRIDGE_POOL.pop(pool_id, None)
    if cached:
        with suppress(Exception):
            await cached[1].close()


async def close_all_pooled_bridges() -> None:
    """Close every pooled bridge -- call on addon shutdown."""
    async with _BRIDGE_POOL_LOCK:
        items = list(_BRIDGE_POOL.values())
        _BRIDGE_POOL.clear()
    for _, bridge in items:
        with suppress(Exception):
            await bridge.close()


'''
    return text.replace(marker, pool_code + marker)


def adapt_ui(text):
    """Patches for ui-src/src/main.jsx: integration picker + form fields for
    sherpa-onnx/Piper, so they're configurable the same way every other
    provider is -- not just accepted by the backend."""

    def replace(old, new):
        nonlocal text
        if text.count(old) != 1:
            raise ValueError("Upstream main.jsx anchor changed: " + old[:70])
        text = text.replace(old, new)

    replace(
        'function providerDefaults(provider) {',
        '''function providerDefaults(provider) {
  if (provider === "cerebras") return { model: "", text_model: "gpt-oss-120b", voice: "" };''',
    )
    replace(
        '      base_url: kind === "ollama" ? "http://localhost:11434/v1" : "",',
        '      base_url: kind === "cerebras" ? "https://api.cerebras.ai/v1" : kind === "ollama" ? "http://localhost:11434/v1" : "",',
    )
    replace(
        '  ["ollama", "Ollama", Cpu],\n  ["local_runtime", "Local runtime", Cpu],\n  ["web_search", "Web Search", Search],',
        '  ["ollama", "Ollama", Cpu],\n  ["groq", "Groq", Cloud],\n  ["cerebras", "Cerebras Cloud", Cloud],\n  ["local_runtime", "Local runtime", Cpu],\n  ["sherpa_onnx", "sherpa-onnx (local STT)", Cpu],\n  ["piper_wyoming", "Piper/Wyoming (local TTS)", Cpu],\n  ["web_search", "Web Search", Search],',
    )
    replace(
        '  stt: ["soniox", "deepgram", "speechmatics", "gradium", "openai_cloud"],',
        '  stt: ["soniox", "deepgram", "speechmatics", "gradium", "openai_cloud", "sherpa_onnx"],',
    )
    replace(
        '  llm: ["openai_cloud", "gemini_cloud", "aws_bedrock", "openai_compatible", "ollama"],',
        '  llm: ["openai_cloud", "gemini_cloud", "aws_bedrock", "openai_compatible", "ollama", "groq", "cerebras"],',
    )
    replace(
        '  tts: ["cartesia", "gradium", "google_cloud_tts", "google_streaming_tts", "elevenlabs", "openai_cloud", "soniox"],',
        '  tts: ["cartesia", "gradium", "google_cloud_tts", "google_streaming_tts", "elevenlabs", "openai_cloud", "soniox", "piper_wyoming"],',
    )
    replace(
        '        "ollama",\n        "local_runtime",\n        "ha_mcp",\n        "mcp_server",\n      ].includes(',
        '        "ollama",\n        "local_runtime",\n        "sherpa_onnx",\n        "piper_wyoming",\n        "ha_mcp",\n        "mcp_server",\n      ].includes(',
    )
    replace(
        '      "anthropic",\n      "azure_openai",\n      "soniox",\n      "deepgram",\n      "cartesia",\n      "gradium",\n      "speechmatics",\n      "elevenlabs",\n      "google_imagen",\n      "fal_image",\n    ].includes(integration.kind)',
        '      "anthropic",\n      "azure_openai",\n      "soniox",\n      "deepgram",\n      "cartesia",\n      "gradium",\n      "speechmatics",\n      "elevenlabs",\n      "google_imagen",\n      "fal_image",\n      "groq",\n      "cerebras",\n    ].includes(integration.kind)',
    )
    replace(
        '  if (integration.kind === "azure_openai") {',
        '''  if (integration.kind === "sherpa_onnx") {
    return (
      <SettingsSection title="sherpa-onnx online-websocket-server">
        <TextSetting
          integration={integration}
          field="base_url"
          label="Host:port"
          placeholder="192.168.1.211:6006"
          updateIntegration={updateIntegration}
          wide
        />
      </SettingsSection>
    );
  }

  if (integration.kind === "piper_wyoming") {
    return (
      <SettingsSection title="Piper (Wyoming protocol)">
        <TextSetting
          integration={integration}
          field="base_url"
          label="Host:port"
          placeholder="192.168.1.211:10200"
          updateIntegration={updateIntegration}
          wide
        />
      </SettingsSection>
    );
  }

  if (integration.kind === "cerebras") {
    return (
      <SettingsSection title="Cerebras Cloud" status={secretStatus(integration, "api_key")}>
        <SecretSetting integration={integration} field="api_key" label="API key" updateIntegration={updateIntegration} />
        <TextSetting integration={integration} field="base_url" label="Base URL" placeholder="https://api.cerebras.ai/v1" updateIntegration={updateIntegration} wide />
        <TextSetting integration={integration} field="default_model" label="Model" placeholder="gpt-oss-120b" updateIntegration={updateIntegration} />
      </SettingsSection>
    );
  }

  if (integration.kind === "groq") {
    return (
      <SettingsSection title="Groq" status={secretStatus(integration, "api_key")}>
        <SecretSetting integration={integration} field="api_key" label="API key" updateIntegration={updateIntegration} />
        <TextSetting
          integration={integration}
          field="default_model"
          label="Model"
          placeholder="llama-3.3-70b-versatile"
          updateIntegration={updateIntegration}
        />
      </SettingsSection>
    );
  }

  if (integration.kind === "azure_openai") {''',
    )
    replace(
        'function TextSetting({ integration, field, label, updateIntegration, wide = false }) {\n  return (\n    <Field label={label} wide={wide}>\n      <input\n        autoComplete="off"\n        value={modelFieldValue(integration, field)}',
        'function TextSetting({ integration, field, label, updateIntegration, wide = false, placeholder }) {\n  return (\n    <Field label={label} wide={wide}>\n      <input\n        autoComplete="off"\n        placeholder={placeholder}\n        value={modelFieldValue(integration, field)}',
    )

    # --- Custom Sentences tab: a nav tab beside Runtime showing which
    # custom_sentences files/intents loaded, which failed, and which bypass
    # the LLM (app/fast_path.py's FastPathReport, read-only, never on the
    # voice-turn path). ---
    replace(
        '  Image as ImageIcon,\n  Mic2,',
        '  Image as ImageIcon,\n  ListChecks,\n  Mic2,',
    )
    replace(
        '          {[\n            ["assistant", t("Assistant"), Bot],\n            ["pipelines", t("Pipelines"), Workflow],\n            ["integrations", t("Integrations"), SlidersHorizontal],\n            ["runtime", t("Runtime"), Settings],\n          ].map(([id, label, Icon]) => (',
        '          {[\n            ["assistant", t("Assistant"), Bot],\n            ["pipelines", t("Pipelines"), Workflow],\n            ["integrations", t("Integrations"), SlidersHorizontal],\n            ["runtime", t("Runtime"), Settings],\n            ["custom_sentences", t("Custom Sentences"), ListChecks],\n          ].map(([id, label, Icon]) => (',
    )
    replace(
        '              {tab === "assistant"\n                ? t("Assistant")\n                : tab === "pipelines"\n                  ? t("Pipelines")\n                  : tab === "integrations"\n                    ? t("Integrations")\n                    : t("Runtime")}',
        '              {tab === "assistant"\n                ? t("Assistant")\n                : tab === "pipelines"\n                  ? t("Pipelines")\n                  : tab === "integrations"\n                    ? t("Integrations")\n                    : tab === "custom_sentences"\n                      ? t("Custom Sentences")\n                      : t("Runtime")}',
    )
    replace(
        '  mcpHistory: appUrl("api/assist/mcp/history"),',
        '  mcpHistory: appUrl("api/assist/mcp/history"),\n  fastPathStatus: appUrl("api/assist/fast_path/status"),',
    )
    replace(
        '  const [mcpHistory, setMcpHistory] = useState({ calls: [] });',
        '  const [mcpHistory, setMcpHistory] = useState({ calls: [] });\n  const [fastPathStatus, setFastPathStatus] = useState({ languages: [] });',
    )
    replace(
        '    const [configResponse, statusResponse, audioResponse, mcpHistoryResponse] = await Promise.all([\n      fetch(API.config),\n      fetch(API.status),\n      fetch(API.audioDebug).catch(() => null),\n      fetch(API.mcpHistory).catch(() => null),\n    ]);',
        '    const [configResponse, statusResponse, audioResponse, mcpHistoryResponse, fastPathStatusResponse] = await Promise.all([\n      fetch(API.config),\n      fetch(API.status),\n      fetch(API.audioDebug).catch(() => null),\n      fetch(API.mcpHistory).catch(() => null),\n      fetch(API.fastPathStatus).catch(() => null),\n    ]);',
    )
    replace(
        '    if (mcpHistoryResponse?.ok) {\n      setMcpHistory(await mcpHistoryResponse.json());\n    }',
        '    if (mcpHistoryResponse?.ok) {\n      setMcpHistory(await mcpHistoryResponse.json());\n    }\n    if (fastPathStatusResponse?.ok) {\n      setFastPathStatus(await fastPathStatusResponse.json());\n    }',
    )
    replace(
        '  async function refreshMcpHistory() {\n    const response = await fetch(API.mcpHistory);\n    if (response.ok) {\n      setMcpHistory(await response.json());\n    }\n  }',
        '''  async function refreshMcpHistory() {
    const response = await fetch(API.mcpHistory);
    if (response.ok) {
      setMcpHistory(await response.json());
    }
  }

  async function refreshFastPathStatus() {
    const response = await fetch(API.fastPathStatus);
    if (response.ok) {
      setFastPathStatus(await response.json());
    }
  }''',
    )
    replace(
        '''        {tab === "runtime" && (
          <RuntimeView
            config={config}
            flow={activeFlow}
            status={status}
            copyEspHomeUrl={copyEspHomeUrl}
            audioDebug={audioDebug}
            refreshAudioDebug={refreshAudioDebug}
            clearAudioDebug={clearAudioDebug}
            mcpHistory={mcpHistory}
            refreshMcpHistory={refreshMcpHistory}
            clearMcpHistory={clearMcpHistory}
            updateConfig={updateConfig}
            updateIntegration={updateIntegration}
            save={save}
            saving={saving}
          />
        )}''',
        '''        {tab === "runtime" && (
          <RuntimeView
            config={config}
            flow={activeFlow}
            status={status}
            copyEspHomeUrl={copyEspHomeUrl}
            audioDebug={audioDebug}
            refreshAudioDebug={refreshAudioDebug}
            clearAudioDebug={clearAudioDebug}
            mcpHistory={mcpHistory}
            refreshMcpHistory={refreshMcpHistory}
            clearMcpHistory={clearMcpHistory}
            updateConfig={updateConfig}
            updateIntegration={updateIntegration}
            save={save}
            saving={saving}
          />
        )}

        {tab === "custom_sentences" && (
          <CustomSentencesView
            fastPathStatus={fastPathStatus}
            refreshFastPathStatus={refreshFastPathStatus}
          />
        )}''',
    )
    replace(
        'createRoot(document.getElementById("root")).render(<App />);',
        '''function CustomSentencesView({ fastPathStatus, refreshFastPathStatus }) {
  const languages = fastPathStatus?.languages || [];
  const enabled = fastPathStatus?.enabled !== false;
  return (
    <div className="runtime-home">
      <section className="panel main-panel">
        <div className="panel-head">
          <div>
            <h3>{t("Custom Sentences")}</h3>
            <span>
              {enabled
                ? t(
                    "Commands are tried by Home Assistant's built-in local agent before the LLM. Install custom sentences and their intent handlers in Home Assistant. The files below describe the limited MCP fallback used when the local agent is not configured; MCP allowlists keep that restricted fallback.",
                  )
                : t("Fast-path matching is turned off -- every sentence goes to the LLM.")}
            </span>
          </div>
          <div className="button-row">
            <span className="status-pill">
              {fastPathStatus?.home_assistant_agent_configured
                ? t("HA local agent configured")
                : t("MCP fallback only")}
            </span>
            <Button icon={RefreshCw} variant="secondary" onClick={refreshFastPathStatus}>
              {t("Refresh")}
            </Button>
          </div>
        </div>
        {languages.length ? (
          languages.map((entry) => <CustomSentencesLanguage key={entry.language} entry={entry} />)
        ) : (
          <div className="empty-state">{t("Nothing to show yet -- press Refresh.")}</div>
        )}
      </section>
    </div>
  );
}

function CustomSentencesLanguage({ entry }) {
  if (entry.error) {
    return (
      <div className="sentence-lang-block">
        <div className="panel-head">
          <div>
            <h4>{entry.language}</h4>
          </div>
          <span className="status-pill error">{t("Failed to load")}</span>
        </div>
        <div className="empty-state">{entry.error}</div>
      </div>
    );
  }
  const files = entry.files || [];
  const allIntents = entry.intents || [];
  const customIntents = allIntents.filter((intent) => intent.source !== "built-in");
  const builtInCount = allIntents.length - customIntents.length;
  const fastPathCount = customIntents.filter((intent) => intent.fast_path_eligible).length;
  const byName = new Map(allIntents.map((intent) => [intent.name, intent]));
  return (
    <div className="sentence-lang-block">
      <div className="panel-head">
        <div>
          <h4>{entry.language.toUpperCase()}</h4>
          <span>
            {files.length} custom_sentences file{files.length === 1 ? "" : "s"} -- {customIntents.length} custom
            intent{customIntents.length === 1 ? "" : "s"}, {fastPathCount} supported by the MCP fallback;
            {" "}{builtInCount} built-in intent{builtInCount === 1 ? "" : "s"} available
          </span>
        </div>
      </div>
      <div className="sentence-file-list">
        {files.length ? (
          files.map((file) => <SentenceFileCard key={file.path} file={file} byName={byName} />)
        ) : (
          <div className="empty-state">
            {t("No custom_sentences files found for this language.")}
          </div>
        )}
      </div>
    </div>
  );
}

function SentenceFileCard({ file, byName }) {
  const shortName = file.path.split(/[\\\\/]/).pop();
  return (
    <div className={`mcp-call ${file.ok ? "ok" : "error"}`}>
      <div className="mcp-call-head">
        <strong>{shortName}</strong>
        <span className={`status-pill ${file.ok ? "ok" : "error"}`}>
          {file.ok ? t("Loaded") : t("Failed")}
        </span>
      </div>
      <small>{file.path}</small>
      {file.ok ? (
        <div className="intent-rows">
          {file.intent_names.map((name) => {
            const info = byName.get(name);
            return (
              <div key={name} className="intent-row">
                <span className="intent-name">{name}</span>
                <span>
                  {info
                    ? `${info.sentence_count} ${t(info.sentence_count === 1 ? "sentence" : "sentences")}`
                    : ""}
                </span>
                <span className={`status-pill ${info?.fast_path_eligible ? "ok" : ""}`}>
                  {info?.fast_path_eligible ? t("MCP fallback supported") : t("HA local agent route")}
                </span>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="empty-state">{file.error}</div>
      )}
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);''',
    )
    return text


def adapt_styles(text):
    """Appends CSS for the Custom Sentences tab, reusing the existing
    panel/status-pill/mcp-call vocabulary rather than inventing a parallel one."""

    marker = "\n.runtime-facts.stacked {"
    if text.count(marker) != 1:
        raise ValueError("Upstream styles.css anchor changed: " + marker.strip())
    addition = '''
.sentence-lang-block,
.sentence-file-list {
  display: grid;
  gap: 12px;
}

.intent-rows {
  display: grid;
  gap: 6px;
  margin-top: 4px;
}

.intent-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  border-top: 1px solid var(--line);
  padding-top: 6px;
  font-size: 13px;
}

.intent-row:first-child {
  border-top: none;
  padding-top: 0;
}

.intent-name {
  font-weight: 700;
  color: var(--ink);
}
'''
    return text.replace(marker, addition + marker)


def build_ui(out_dir: Path) -> None:
    """Patch and rebuild the web UI bundle (ui-src -> app/ui)."""

    path = out_dir / 'ui-src/src/main.jsx'
    path.write_text(adapt_ui(path.read_text(encoding='utf-8')), encoding='utf-8')
    styles_path = out_dir / 'ui-src/src/styles.css'
    styles_path.write_text(adapt_styles(styles_path.read_text(encoding='utf-8')), encoding='utf-8')
    ui_src = out_dir / 'ui-src'
    pnpm = shutil.which('pnpm')
    if not pnpm:
        raise RuntimeError(
            'pnpm not found on PATH -- required to rebuild the UI bundle '
            '(ui-src -> app/ui) after patching main.jsx. Install pnpm and re-run.'
        )
    subprocess.check_call([pnpm, 'install', '--frozen-lockfile'], cwd=str(ui_src))
    subprocess.check_call([pnpm, 'run', 'build'], cwd=str(ui_src))
    # node_modules is a build-time dependency of this step alone; the add-on
    # itself runs from app/ui's compiled output and never needs it.
    node_modules = ui_src / 'node_modules'
    if node_modules.exists():
        shutil.rmtree(node_modules)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--out', type=Path, default=ROOT/'dist/pipecat-assist-echomuse-dev')
    args = parser.parse_args()
    sha = subprocess.check_output(['git', '-C', str(args.source), 'rev-parse', 'HEAD'], text=True).strip()
    if sha != PIN:
        parser.error('Expected pinned upstream commit ' + PIN)
    source = args.source/'addons/pipecat_assist'
    # This command only replaces its own generated local add-on folder.
    if args.out.exists():
        shutil.rmtree(args.out)
    shutil.copytree(source, args.out, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.env'))
    shutil.copyfile(args.source/'LICENSE', args.out/'LICENSE')
    path = args.out/'app/main.py'
    path.write_text(adapt(path.read_text(encoding='utf-8')), encoding='utf-8')
    path = args.out/'app/va_pipecat_protocol.py'
    protocol = path.read_text(encoding='utf-8')
    old = 'phase_extra = {"terminal": True} if terminal else {"follow_up": True}'
    assert protocol.count(old) == 1
    protocol = protocol.replace('    tool_start_pending: bool = False', '    tool_start_pending: bool = False\n    continuation_requested: bool = False')
    protocol = protocol.replace('            self.tool_start_pending = False\n            return "wake"', '            self.tool_start_pending = False\n            self.continuation_requested = False\n            return "wake"')
    protocol = protocol.replace('            self.tool_start_pending = False\n            return "stop"', '            self.tool_start_pending = False\n            self.continuation_requested = False\n            return "stop"')
    protocol = protocol.replace(old, 'phase_extra = {"terminal": True} if terminal else {"follow_up": True, "continuation_question": self.continuation_requested}')
    protocol = protocol.replace('            self.active = not terminal\n            self.stop_requested = False', '            continuation_question = self.continuation_requested\n            self.continuation_requested = False\n            self.active = not terminal\n            self.stop_requested = False')
    protocol = protocol.replace('phase_extra = {"terminal": True} if terminal else {"follow_up": True, "continuation_question": self.continuation_requested}', 'phase_extra = {"terminal": True} if terminal else {"follow_up": True, "continuation_question": continuation_question}')
    protocol = protocol.replace('self.phase_message("listening", follow_up=True)', 'self.phase_message("listening", follow_up=True, continuation_question=continuation_question)')
    path.write_text(protocol, encoding='utf-8')
    path = args.out/'app/mcp_bridge.py'
    bridge = path.read_text(encoding='utf-8')
    old_call = """            session = self.client._ensure_connected()  # Pipecat exposes no public call_tool yet.
            result = await session.call_tool(name, arguments=arguments)
            chunks: list[str] = []
            for content in getattr(result, "content", []) or []:
                text = getattr(content, "text", None)
                if text:
                    chunks.append(text)
            text_result = "\\n".join(chunks) if chunks else "Tool returned no text content."
"""
    new_call = """            session = self.client._ensure_connected()  # Pipecat exposes no public call_tool yet.

            async def _call(call_arguments):
                result = await session.call_tool(name, arguments=call_arguments)
                chunks: list[str] = []
                for content in getattr(result, "content", []) or []:
                    text = getattr(content, "text", None)
                    if text:
                        chunks.append(text)
                return "\\n".join(chunks) if chunks else "Tool returned no text content."

            text_result = await echomuse.call_with_area_fallback(_call, arguments)
"""
    if bridge.count(old_call) != 1:
        raise ValueError("Upstream MCP bridge anchor changed")
    bridge = bridge.replace(old_call, new_call)
    bridge = bridge.replace("from __future__ import annotations\n", "from __future__ import annotations\n\nfrom app import echomuse\n", 1)
    bridge = adapt_mcp_bridge_pool(bridge)
    path.write_text(bridge, encoding='utf-8')
    path = args.out/'app/config.py'
    path.write_text(adapt_config(path.read_text(encoding='utf-8')), encoding='utf-8')
    path = args.out/'app/text_agent.py'
    path.write_text(adapt_text_agent(path.read_text(encoding='utf-8')), encoding='utf-8')
    shutil.copyfile(Path(__file__).with_name('echomuse.py'), args.out/'app/echomuse.py')
    for name in ('sherpa_stt.py', 'piper_wyoming_tts.py', 'energy_vad.py', 'fast_path.py', 'local_commands.py', 'tool_scope.py', 'tool_selector.py',
                 'schedule_language.py', 'schedule_dialogue.py', 'schedule_select.py', 'schedule_mcp.py', 'weather_commands.py'):
        shutil.copyfile(Path(__file__).with_name(name), args.out/'app'/name)
    custom_sentences_src = Path(__file__).with_name('custom_sentences')
    custom_sentences_dst = args.out/'custom_sentences'
    if custom_sentences_dst.exists():
        shutil.rmtree(custom_sentences_dst)
    shutil.copytree(custom_sentences_src, custom_sentences_dst)
    pyproject_path = args.out/'pyproject.toml'
    pyproject = pyproject_path.read_text(encoding='utf-8')
    anchor = '    "websockets>=13.0",\n]'
    if pyproject.count(anchor) != 1:
        raise ValueError("Upstream pyproject.toml anchor changed: " + anchor)
    pyproject = pyproject.replace(anchor, '''    "websockets>=13.0",
    # Local STT (sherpa-onnx online-websocket-server) and TTS (Piper) providers
    "numpy>=1.26.0",
    "wyoming>=1.5.0",
    # Fast-path intent matching: the same engine and built-in English
    # sentence templates Home Assistant's own Assist pipeline uses for
    # custom_sentences, so a transcript matching one of its sentence
    # templates can skip the LLM entirely (app/fast_path.py).
    "hassil>=2.0.0",
    "home-assistant-intents>=2025.1.0",
    "pyyaml>=6.0",
    "tzdata>=2025.1",
]''')
    pyproject_path.write_text(pyproject, encoding='utf-8')
    build_ui(args.out)
    version = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', '--short', 'HEAD'], text=True).strip()
    path = args.out/'config.yaml'
    config = path.read_text(encoding='utf-8')
    config = re.sub(r'^image:.*\n', '', config, flags=re.M)
    config = re.sub(r'^name:.*$', 'name: Pipecat Assist (EchoMuse dev)', config, flags=re.M)
    config = re.sub(r'^slug:.*$', 'slug: pipecat_assist_echomuse', config, flags=re.M)
    config = re.sub(r'^version:.*$', f'version: "0.1.80-em-{version}"', config, flags=re.M)
    if 'homeassistant_config:ro' not in config:
        # Read-only access to HA's own config dir, mounted at /homeassistant,
        # so the fast-path intent matcher can load the user's actual
        # custom_sentences/<language>/*.yaml -- the same files and format
        # HA's own Assist pipeline reads, parsed with the same hassil
        # library. Read-only: this add-on never writes to HA's config.
        config = re.sub(
            r'^(hassio_role:.*)$',
            r'\1\nmap:\n  - homeassistant_config:ro',
            config,
            flags=re.M,
        )
    path.write_text(config, encoding='utf-8')
    print(args.out)


if __name__ == '__main__':
    main()
