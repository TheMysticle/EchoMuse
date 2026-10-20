"""Route completed utterances to HA's built-in local conversation agent.

The native route uses local_commands.py and HA's actual configured handlers.
The remaining grammar/report helpers implement a restricted MCP fallback:
skip the LLM entirely for a narrow, exact
set of HA's own built-in voice commands, using the SAME grammar and matching
engine Home Assistant's Assist pipeline uses for its "custom sentences"
feature (the `hassil` library plus the `home-assistant-intents` package,
which ships the built-in English sentence templates for intents like
HassTurnOn/HassTurnOff/HassLightSet). A transcript that matches one of these
templates goes straight to the matching Home Assistant MCP tool and a
deterministic confirmation -- no LLM round trip, so none of the multi-step
tool-calling search a free-text query can trigger (see FAST_PATH_INTENTS
below for why that set is narrow).

Why a measured, conservative allowlist rather than every built-in intent:
state QUERIES (HassGetState, HassClimateGetTemperature, GetWeather, ...) need
the model's own judgement to phrase a natural answer from structured data,
The configured Get weather script has a separate, validated local forecast
formatter (weather_commands.py). Other state-query payloads still need the LLM.
An ambiguous reference ("turn it off", ambiguous area/name) should fall
through to the LLM rather than be forced into a hard match -- hassil already
does this for free: it returns no match (or an unmatched_entities result)
for anything that is not a clean, grammatical fit, and this module only
acts on a clean match with zero unmatched entities.

Also loads custom_sentences: Home Assistant's own format
(config/custom_sentences/<language>/*.yaml), read from this add-on's
mounted copy of /config (see config.yaml's `homeassistant_config:ro` map)
when running as a real HA add-on, OR from a locally configured directory
for standalone/test deployments. Same loader, same hassil engine, same
format -- so sentences authored for HA's own Assist work here unchanged.
"""

from __future__ import annotations

import importlib.util
import asyncio
import json
import os
import sys
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import home_assistant_intents
import yaml
from hassil import Intents, RecognizeResult, TextSlotList, errors, recognize
from hassil.util import merge_dict
from loguru import logger

from pipecat.frames.frames import Frame, LLMContextFrame, TTSSpeakFrame, VADUserStartedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


def _load_sibling(name: str):
    """Load a module next to this file by its own path, under a fixed name
    in sys.modules -- so its OWN sibling imports (e.g. schedule_dialogue.py's
    `from schedule_language import ...`) resolve to the exact same instance
    regardless of whether this file is itself loaded as a package submodule
    (the real add-on, `app.fast_path`) or standalone (the test suite). A
    caller may pre-register one of these names in sys.modules before this
    module is imported, to supply a double."""

    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


schedule_language = _load_sibling("schedule_language")
schedule_dialogue = _load_sibling("schedule_dialogue")
schedule_select = _load_sibling("schedule_select")
schedule_mcp = _load_sibling("schedule_mcp")
weather_commands = _load_sibling("weather_commands")
tool_selector = _load_sibling("tool_selector")

# EchoMuseCreate/List/Action, handled locally (no LLM) when the parse is
# clean -- see FastPathStep._start_schedule. Never Hass*: those always skip
# the fast-path matcher entirely and go to HA's own local agent instead.
SCHEDULE_INTENTS = frozenset({"EchoMuseCreate", "EchoMuseList", "EchoMuseAction"})

# EchoMuseAction's MCP tool schema supports exactly these; "cancel_all" and
# "complete" have no MCP equivalent (the device REST API the native HA
# handler uses supports them, but that endpoint is not reachable from here),
# so those fall through to the LLM's own, equally limited, tool.
SCHEDULE_ACTIONS = frozenset({"cancel", "dismiss", "snooze", "pause", "resume", "enable", "disable"})

# Action intents only: each has a clean action_done/action_failed result
# shape from HA's MCP intent tools (data.success / data.failed lists), so a
# confirmation can be built without re-deriving the natural-language answer
# a query intent needs. Extend this set only for intents with the same
# action_done shape -- never for a query intent.
FAST_PATH_INTENTS = frozenset(
    {
        "HassTurnOn",
        "HassTurnOff",
        "HassLightSet",
        "HassMediaPause",
        "HassMediaUnpause",
        "HassMediaPlayerMute",
        "HassMediaPlayerUnmute",
        "HassMediaNext",
        "HassMediaPrevious",
        "HassClimateSetTemperature",
    }
)

_VERBS: dict[str, str] = {
    "HassTurnOn": "Turned on",
    "HassTurnOff": "Turned off",
    "HassLightSet": "Set",
    "HassMediaPause": "Paused",
    "HassMediaUnpause": "Resumed",
    "HassMediaPlayerMute": "Muted",
    "HassMediaPlayerUnmute": "Unmuted",
    "HassMediaNext": "Skipped",
    "HassMediaPrevious": "Went back on",
    "HassClimateSetTemperature": "Set the temperature of",
}

SLOT_LIST_TTL_SECONDS = 300  # live entity/area names drift as little as HA's own cache


@dataclass
class LoadedFile:
    """One custom_sentences file this process tried to load, for the Custom
    Sentences status tab -- never read on the turn path, only built once per
    language alongside the compiled Intents and cached on FastPathState."""

    path: str
    ok: bool
    intent_names: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class IntentStatus:
    """One intent in the merged grammar: where it came from (a custom file's
    path, or "built-in" for HA's own data) and whether a match on it skips
    the LLM (FAST_PATH_INTENTS) -- not every custom sentence is meant to:
    EchoMuseSpotifySearchAndPlay still goes to the LLM to pick a search
    query, then calls a tool that does its own local, fast catalogue search
    (custom_components/echomuse_spotify/search.py) -- not fast-path-eligible
    is not the same claim as slow."""

    name: str
    source: str
    sentence_count: int
    fast_path_eligible: bool


@dataclass
class FastPathReport:
    language: str
    custom_sentence_dirs: list[str]
    files: list[LoadedFile]
    intents: list[IntentStatus]


def load_intents(language: str, custom_sentence_dirs: list[Path]) -> tuple[Intents, FastPathReport]:
    """Build HA's own built-in intents for `language`, merged with any
    custom_sentences YAML files found in `custom_sentence_dirs` (checked in
    order; HA's own format: `{language: ..., intents: {...}, lists: {...}}`,
    the same files `custom_sentences/<language>/*.yaml` under HA's config
    directory, or any directory holding the same format).

    Also returns a FastPathReport describing exactly what was found: which
    files loaded, which failed and why, and which intents the merged grammar
    now recognizes -- the data behind the Custom Sentences status tab.
    """

    data = home_assistant_intents.get_intents(language)
    if data is None:
        raise RuntimeError(f"home-assistant-intents has no built-in data for language {language!r}")
    merged = deepcopy(data)
    files: list[LoadedFile] = []
    custom_sources: dict[str, str] = {}

    for directory in custom_sentence_dirs:
        lang_dir = directory / language
        if not lang_dir.is_dir():
            continue
        for path in sorted(lang_dir.glob("*.yaml")):
            try:
                with path.open(encoding="utf-8") as file:
                    custom = yaml.safe_load(file)
                if isinstance(custom, dict):
                    names = sorted((custom.get("intents") or {}).keys())
                    candidate = deepcopy(merged)
                    merge_dict(candidate, custom)
                    Intents.from_dict(candidate)  # one bad file must not disable all matching
                    merged = candidate
                    for name in names:
                        custom_sources[name] = str(path)
                    files.append(LoadedFile(path=str(path), ok=True, intent_names=names))
                    logger.info("Fast-path: loaded custom_sentences {}", path)
                else:
                    files.append(LoadedFile(path=str(path), ok=False, error="not a YAML mapping"))
                    logger.warning("Fast-path: {} is not a YAML mapping, skipped", path)
            except Exception as err:
                files.append(LoadedFile(path=str(path), ok=False, error=str(err)))
                logger.warning("Fast-path: could not load custom_sentences {}: {}", path, err)

    intents = Intents.from_dict(merged)
    statuses = [
        IntentStatus(
            name=name,
            source=custom_sources.get(name, "built-in"),
            sentence_count=sum(len(block.get("sentences", [])) for block in (intent_data.get("data") or [])),
            fast_path_eligible=name in FAST_PATH_INTENTS or name in {"EchoMuseWeatherForecast", "EchoMuseRainForecast"},
        )
        for name, intent_data in (merged.get("intents") or {}).items()
    ]
    report = FastPathReport(
        language=language,
        custom_sentence_dirs=[str(d) for d in custom_sentence_dirs],
        files=files,
        intents=sorted(statuses, key=lambda s: s.name),
    )
    return intents, report


def _split_names(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def parse_live_context(result_text: str) -> tuple[list[str], list[str]]:
    """Extract (entity names, area names) out of a GetLiveContext MCP result.

    The result is `{"result": "<yaml text>"}`; the yaml text is a list of
    entity records with a `names` field (comma-separated aliases) and an
    `areas` field. Pure text parsing -- no network, no MCP types -- so it is
    unit-testable without a live Home Assistant.
    """

    import json

    names: set[str] = set()
    areas: set[str] = set()
    try:
        outer = json.loads(result_text)
        text = outer.get("result", "") if isinstance(outer, dict) else ""
        # The first line is a free-text summary ("Live Context: An overview
        # of ... this smart home:") which is not itself valid YAML -- the
        # actual data is the "- names: ..." list that follows it.
        _, _, list_text = text.partition("\n- ")
        inner = yaml.safe_load("- " + list_text) if list_text else None
        records = inner if isinstance(inner, list) else []
        for record in records:
            if not isinstance(record, dict):
                continue
            raw_names = record.get("names")
            if isinstance(raw_names, str):
                names.update(_split_names(raw_names))
            elif isinstance(raw_names, list):
                names.update(str(n).strip() for n in raw_names if str(n).strip())
            raw_areas = record.get("areas")
            if isinstance(raw_areas, str):
                areas.update(_split_names(raw_areas))
            elif isinstance(raw_areas, list):
                areas.update(str(a).strip() for a in raw_areas if str(a).strip())
    except Exception as err:
        logger.warning("Fast-path: could not parse live context for slot lists: {}", err)
    return sorted(names), sorted(areas)


@dataclass
class FastPathState:
    """Compiled intents + live name/area slot lists, cached per flow."""

    intents: Intents
    slot_lists: dict[str, TextSlotList] = field(default_factory=dict)
    slot_lists_at: float = 0.0
    report: FastPathReport | None = None

    def slot_lists_stale(self) -> bool:
        return (time.time() - self.slot_lists_at) > SLOT_LIST_TTL_SECONDS

    def set_slot_lists(self, names: list[str], areas: list[str], floors: list[str] | None = None) -> None:
        self.slot_lists = {
            "name": TextSlotList.from_strings(names, name="name"),
            "area": TextSlotList.from_strings(areas, name="area"),
            # Most homes have no floor list at all; an empty one still lets
            # a floor-shaped sentence template fail to match cleanly
            # instead of raising, which is what match() below guards.
            "floor": TextSlotList.from_strings(floors or [], name="floor"),
        }
        self.slot_lists_at = time.time()


def _recognize_any(state: FastPathState, text: str, language: str) -> RecognizeResult | None:
    """Recognize `text` against the full merged grammar, independent of
    FAST_PATH_INTENTS -- used to tell EchoMuse's own custom intents
    (anything not prefixed Hass, e.g. EchoMuseCreate/EchoMuseList/
    EchoMuseAction) apart from HA's built-ins, so the caller can keep those
    off HA's local conversation agent before it even gets a chance to
    answer them (see FastPathStep.process_frame)."""

    try:
        result = recognize(
            text,
            state.intents,
            slot_lists=state.slot_lists,
            language=language,
        )
    except errors.HassilError:
        # A sentence template referencing a slot list this home has none of
        # (e.g. {floor} with no floors) is a template that cannot match
        # here -- the same as no match, not a crash.
        return None
    if result is None:
        return None
    if result.unmatched_entities:
        return None
    return result


def match(state: FastPathState, text: str, language: str) -> RecognizeResult | None:
    """Try to recognize `text` as one of the allowed fast-path intents.

    Returns None on no match, an ambiguous/unmatched entity, or an intent
    outside FAST_PATH_INTENTS -- all of which fall through to the LLM.
    """

    result = _recognize_any(state, text, language)
    if result is None or result.intent.name not in FAST_PATH_INTENTS:
        return None
    return result


def slots_to_tool_args(result: RecognizeResult) -> dict[str, Any]:
    """Map hassil's matched entities onto MCP tool arguments.

    HA's MCP intent tools are generated from the same intent definitions
    hassil matches against, so slot names (name/area/domain/...) already
    equal the tool's own parameter names -- confirmed against the live
    schema for HassTurnOff/HassLightSet (`name`, `area`, `floor`, `domain`,
    `device_class`, `brightness`, `color`, `temperature`).
    """

    args: dict[str, Any] = {}
    for slot_name, entity in result.entities.items():
        if entity.value is None or entity.value == "":
            continue
        args[slot_name] = ([entity.value] if slot_name in {"domain", "device_class"}
                           and not isinstance(entity.value, list) else entity.value)
    return args


def build_confirmation(intent_name: str, args: dict[str, Any], tool_result: dict[str, Any]) -> str | None:
    """Build a short spoken confirmation from an HA intent tool's structured
    result, or None if the result shape is not one this can safely confirm
    (in which case the caller must fall through to the LLM instead of
    guessing at what happened)."""

    speech = ((tool_result.get("speech") or {}).get("plain") or {}).get("speech")
    if speech:
        return str(speech)
    if tool_result.get("response_type") == "error" or (tool_result.get("data") or {}).get("failed"):
        return "Sorry, that command failed."
    if tool_result.get("response_type") != "action_done":
        return None
    data = tool_result.get("data") or {}
    if data.get("failed"):
        return None
    success = data.get("success") or []
    verb = _VERBS.get(intent_name)
    if not verb:
        return None

    names = [item.get("name") for item in success if isinstance(item, dict) and item.get("name")]
    target = names[0] if len(names) == 1 else (f"{len(names)} things" if len(names) > 1 else args.get("area") or args.get("name"))
    if not target:
        return f"{verb}."
    return f"{verb} {target}."


def tool_names_for_fast_path(tool_names: list[str]) -> dict[str, str]:
    """Map each FAST_PATH_INTENTS name to the real MCP tool name that
    implements it, by the tool name's own last `__`-separated segment (HA's
    MCP server exposes intents as `assist__<category>__<IntentName>`)."""

    mapping: dict[str, str] = {}
    for tool_name in tool_names:
        intent_name = tool_name.rsplit("__", 1)[-1]
        if intent_name in FAST_PATH_INTENTS:
            mapping[intent_name] = tool_name
    return mapping


def report_to_dict(report: FastPathReport) -> dict[str, Any]:
    """JSON-safe view of a FastPathReport, for the Custom Sentences status
    endpoint -- plain dataclass->dict, no recomputation."""

    return asdict(report)


class FastPathStep(FrameProcessor):
    """Route a completed user turn to HA's local agent before the LLM.

    Without a native HA API connection, retain the narrow hassil/MCP fallback.
    Responses go through the configured TTS and assistant context aggregation.

    Safety property: once a tool call is attempted, this step is ALWAYS
    terminal for the turn (the frame is absorbed, never also forwarded to
    the LLM) even if the call or the confirmation-building fails, because a
    real-world action may already have happened; forwarding on to the LLM
    at that point risks the same command being acted on twice.
    """

    def __init__(
        self,
        *,
        state: FastPathState | None,
        language: str,
        bridge: Any,
        tool_names: dict[str, str],
        local_commands: Any = None,
        protocol: Any = None,
        context: Any = None,
        timezone: str | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._state = state
        self._language = language
        self._bridge = bridge
        self._tool_names = tool_names
        self._local_commands = local_commands
        self._protocol = protocol
        self._context = context
        self._weather_tool = weather_commands.select_tool(context) if bridge is not None else None
        # Matches em_workflow_tools.handle()'s own fallback for the same
        # question; no session plumbing exists yet to carry the satellite's
        # real configured timezone over this transport (see schedule_mcp.py).
        self._timezone = timezone or os.environ.get("TZ") or "UTC"
        self._pending_schedule: dict[str, Any] | None = None
        self._last_created: dict[str, str] = {}
        self._awaiting_user_context = False
        self._user_baseline = self._user_count(context.get_messages()) if context is not None else 0
        self._last_routed_user_count = self._user_baseline

    @staticmethod
    def _user_count(messages):
        # Gemini appends LLMSpecificMessage objects (for example thought
        # signatures) alongside ordinary messages after a real model turn.
        return sum(isinstance(message, dict) and message.get("role") == "user" for message in messages)

    async def cleanup(self):
        try:
            close = getattr(self._local_commands, "aclose", None)
            if close is not None:
                await close()
        finally:
            await super().cleanup()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._awaiting_user_context = True
            if self._context is not None:
                self._user_baseline = self._user_count(self._context.get_messages())

        # Run AFTER user aggregation: several finalized STT segments may belong
        # to one utterance. Absorbing transcripts before the aggregator leaves
        # its turn open and can execute a partial command prematurely.
        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM:
            messages = frame.context.get_messages()
            last = messages[-1] if messages else {}
            user_count = self._user_count(messages)
            if self._awaiting_user_context and user_count <= self._user_baseline:
                logger.warning("Ignoring inference for an utterance without a new user transcript")
                return
            text = last.get("content") if isinstance(last, dict) and last.get("role") == "user" else None
            if isinstance(text, str) and text.strip():
                if user_count <= self._last_routed_user_count:
                    logger.warning("Ignoring duplicate inference for the same user transcript")
                    return
                self._last_routed_user_count = user_count
                self._awaiting_user_context = False
                if self._pending_schedule is not None:
                    # Mid clarification for a local EchoMuseCreate: this
                    # transcript is the answer, never a fresh command, and
                    # must not be matched against anything else first.
                    if await self._continue_schedule_clarification(text.strip()):
                        return
                # Share the exact chat predicate used by tool scoping. Preserve
                # pending local clarifications above; they own the next answer.
                if (len(text) <= 4000 and tool_selector.select_tools(
                        text, [], self._language)['mode'] == 'chat'):
                    logger.info('Local commands: clear conversation; bypassing HA')
                    await self.push_frame(frame, direction)
                    return
                skip_local_agent = weather_commands.requires_forecast(text, self._language)
                if self._weather_tool:
                    try:
                        request = weather_commands.parse_request(text.strip(), self._language)
                    except Exception as err:
                        logger.warning('Local weather grammar unavailable; retaining fallback: {}', err)
                        request = None
                    if request is not None:
                        # Do not let HA answer a future forecast with current
                        # conditions if the local formatter cannot handle it.
                        skip_local_agent = True
                        try:
                            async def timezone():
                                if request.day in weather_commands.DAYS:
                                    return None
                                load = getattr(self._local_commands, 'weather_timezone', None)
                                return await load() if load else None
                            raw, zone = await asyncio.gather(
                                self._bridge.call_tool(self._weather_tool, {}), timezone())
                            speech = weather_commands.forecast_reply(raw, request, timezone=zone)
                        except Exception as err:
                            logger.warning('Local forecast unavailable; using configured LLM: {}', err)
                            speech = None
                        if speech is not None:
                            logger.info('Local forecast response supplied by configured Get weather tool')
                            await self.push_frame(TTSSpeakFrame(text=speech), direction)
                            return
                own_result = None
                if self._state is not None:
                    try:
                        own_result = _recognize_any(self._state, text.strip(), self._language)
                    except Exception as err:
                        logger.warning("Fast-path: own-intent matching failed, trying HA's agent: {}", err)
                        own_result = None
                    if own_result is not None and not own_result.intent.name.startswith("Hass"):
                        # EchoMuse's own scheduler (timers/alarms/reminders)
                        # must always be driven by the LLM's EchoMuseCreate
                        # tool -- HA's competing built-in intent (e.g. its
                        # own bare timer intent) must never get first crack
                        # at answering or announcing through the satellite.
                        skip_local_agent = True
                        logger.info(
                            "Fast-path: {!r} matched EchoMuse intent {} -- routing straight to the LLM",
                            text,
                            own_result.intent.name,
                        )
                if self._local_commands is not None and not skip_local_agent:
                    reply = await self._local_commands.process(text.strip())
                    if reply is not None:
                        if self._protocol is not None:
                            self._protocol.continuation_requested = reply.continue_conversation
                        logger.info("Local command response supplied by the Home Assistant route")
                        await self.push_frame(TTSSpeakFrame(text=reply.text), direction)
                        return
                if (self._bridge is not None and own_result is not None
                        and own_result.intent.name in SCHEDULE_INTENTS):
                    try:
                        if await self._start_schedule(own_result, text.strip()):
                            return
                    except Exception as err:
                        # A parse that looks uncertain, or any unexpected
                        # failure, falls through to the LLM's identical
                        # tools rather than guessing -- same posture as the
                        # rest of this module (see class docstring).
                        logger.warning("Fast-path: local schedule handling failed, falling through to the LLM: {}", err)
                if await self._try_fast_path(text.strip()):
                    return

        await self.push_frame(frame, direction)

    async def _try_fast_path(self, text: str) -> bool:
        if self._state is None:
            return False
        try:
            result = match(self._state, text, self._language)
        except Exception as err:
            logger.warning("Fast-path: matching failed, falling through to LLM: {}", err)
            return False
        if result is None:
            return False

        tool_name = self._tool_names.get(result.intent.name)
        if not tool_name:
            return False

        # A tool call may have real-world effects from here on -- this turn
        # is handled regardless of what happens next (see class docstring).
        args = slots_to_tool_args(result)
        try:
            raw = await self._bridge.call_tool(tool_name, args)
        except Exception as err:
            # The action may have completed before the connection failed.
            logger.warning("Fast-path: tool call {} failed: {}", tool_name, err)
            confirmation = "Sorry, that didn't work."
        else:
            # The call completed without raising, so HA accepted it, even
            # when its content is not the JSON action_done/data shape (e.g.
            # a no-op on an entity already in the target state can return
            # "Tool returned no text content." -- that is not a failure).
            try:
                tool_result = json.loads(raw)
                confirmation = build_confirmation(result.intent.name, args, tool_result)
            except (json.JSONDecodeError, TypeError, AttributeError):
                confirmation = None
            if confirmation is None:
                confirmation = "Sorry, I couldn't confirm whether that command completed."

        logger.info("Fast-path hit: {!r} -> {} ({}) args={}", text, result.intent.name, tool_name, args)
        await self.push_frame(TTSSpeakFrame(text=confirmation), FrameDirection.DOWNSTREAM)
        return True

    async def _start_schedule(self, result: RecognizeResult, text: str) -> bool:
        """EchoMuseCreate/List/Action, parsed and acted on locally -- no LLM
        round trip, matching custom_components/echomuse_schedule/intents.py's
        Handler.async_handle(). Returns False whenever no tool call has been
        attempted (an uncertain parse, or an action with no MCP equivalent),
        so the caller can fall through to the LLM's own identical tools."""

        slots = {name: entity.value for name, entity in result.entities.items()
                 if entity.value not in (None, "")}
        kind = slots.get("kind", "timer")
        intent_name = result.intent.name
        if intent_name == "EchoMuseList":
            return await self._run_schedule_list(kind, slots)
        if intent_name == "EchoMuseAction":
            return await self._run_schedule_action(kind, slots)

        # EchoMuseCreate. Mirrors intents.py's raw_request assembly: a
        # temporal prefix before "remind me" belongs after its message; an
        # alarm's time introducer goes first.
        raw_request = (slots.get("request", "") + " " + slots.get("prefix", "")).strip()
        if slots.get("when"):
            if slots.get("prefix"):
                raw_request = " ".join((slots.get("request", ""), slots["when"], slots["prefix"]))
            else:
                raw_request = (slots["when"] + " " + slots.get("request", "")).strip()
        reference = datetime.now(ZoneInfo(self._timezone))
        try:
            item = schedule_language.parse_item(kind, raw_request, self._timezone, now=reference)
        except schedule_language.Clarification as err:
            self._pending_schedule = {
                "kind": kind, "request": raw_request, "target": slots.get("target"),
                "field": err.field, "reference": reference, "attempts": 1,
            }
            if self._protocol is not None:
                self._protocol.continuation_requested = True
            logger.info("Fast-path local schedule: asking for {} ({!r})", err.field, raw_request)
            await self.push_frame(TTSSpeakFrame(text=str(err)), FrameDirection.DOWNSTREAM)
            return True
        except ValueError as err:
            # A definite, known-bad input (e.g. two conflicting dates) --
            # the native handler speaks this rather than falling back, and
            # so do we: the parse was not uncertain, it was simply invalid.
            await self.push_frame(TTSSpeakFrame(text=str(err)), FrameDirection.DOWNSTREAM)
            return True
        if slots.get("target"):
            item["label"] = slots["target"]
        return await self._create_schedule_item(kind, item)

    async def _continue_schedule_clarification(self, text: str) -> bool:
        pending = self._pending_schedule
        stripped = text.strip().rstrip(".?!").lower()
        if stripped in schedule_dialogue.CANCEL:
            kind = pending["kind"]
            self._pending_schedule = None
            await self.push_frame(TTSSpeakFrame(text=f"Okay, I haven't set the {kind}."), FrameDirection.DOWNSTREAM)
            return True
        candidate = None
        try:
            candidate = schedule_dialogue.complete_request(pending["request"], text, pending["field"])
            item = schedule_language.parse_item(pending["kind"], candidate, self._timezone, now=pending["reference"])
        except schedule_language.Clarification as err:
            # Preserve accepted details (e.g. tomorrow + 8 -> AM/PM).
            if candidate is not None:
                pending["request"] = candidate
            pending["field"] = err.field
            return await self._ask_again_or_give_up(pending, str(err))
        except ValueError as err:
            return await self._ask_again_or_give_up(pending, str(err))
        if pending.get("target"):
            item["label"] = pending["target"]
        self._pending_schedule = None
        return await self._create_schedule_item(pending["kind"], item)

    async def _ask_again_or_give_up(self, pending: dict, question: str) -> bool:
        pending["attempts"] += 1
        if pending["attempts"] > 4:
            kind = pending["kind"]
            field = pending["field"]
            self._pending_schedule = None
            detail = {"message": "the reminder text", "duration": "a duration"}.get(field, "a complete time")
            await self.push_frame(
                TTSSpeakFrame(text=f"I haven't set the {kind}. Please try again with {detail}."),
                FrameDirection.DOWNSTREAM,
            )
            return True
        if self._protocol is not None:
            self._protocol.continuation_requested = True
        await self.push_frame(TTSSpeakFrame(text=question), FrameDirection.DOWNSTREAM)
        return True

    async def _create_schedule_item(self, kind: str, item: dict) -> bool:
        now = datetime.now(ZoneInfo(item["timezone"]))
        try:
            args = schedule_mcp.to_mcp_args(kind, item, now)
        except Exception as err:
            # Nothing has been attempted yet -- safe to let the LLM retry.
            logger.warning("Local schedule: could not build MCP arguments, falling through to the LLM: {}", err)
            return False
        # A tool call may have real-world effects from here on -- this turn
        # is handled regardless of what happens next (see class docstring).
        try:
            raw = await self._bridge.call_tool("EchoMuseCreate", args)
            content = json.loads(raw)
            if not isinstance(content, dict) or "snapshot" not in content:
                raise ValueError(content if isinstance(content, str) else "Unexpected scheduler response")
        except Exception as err:
            logger.warning("Local schedule: create failed: {}", err)
            await self.push_frame(TTSSpeakFrame(text="Sorry, I couldn't set that."), FrameDirection.DOWNSTREAM)
            return True
        items = content["snapshot"].get("items") or []
        if items:
            self._last_created[kind] = items[-1]["id"]
        text = schedule_dialogue.created_speech(kind, item, content)
        logger.info("Fast-path local schedule: created {} ({!r})", kind, item.get("label"))
        await self.push_frame(TTSSpeakFrame(text=text), FrameDirection.DOWNSTREAM)
        return True

    async def _run_schedule_list(self, kind: str, slots: dict) -> bool:
        args = {"kind": kind} if kind else {}
        if slots.get("date"):
            # The speaker filters by day; reading out every reminder to a
            # question about one day is the fault this exists to prevent.
            day = schedule_language.resolve_day(slots["date"], datetime.now(ZoneInfo(self._timezone)))
            if day is None:
                return False
            args["date"] = day
        try:
            raw = await self._bridge.call_tool("EchoMuseList", args)
            content = json.loads(raw)
            if not isinstance(content, dict) or "snapshot" not in content:
                raise ValueError(content if isinstance(content, str) else "Unexpected scheduler response")
        except Exception as err:
            logger.warning("Local schedule: list failed, falling through to the LLM: {}", err)
            return False
        data = content["snapshot"]
        items = [i for i in data["items"] if i["kind"] == kind]
        target = slots.get("target")
        if target:
            try:
                items = [schedule_select.select(data, kind, target, slots, recent=self._last_created,
                                                 now=datetime.now(ZoneInfo(self._timezone)))]
            except ValueError as err:
                await self.push_frame(TTSSpeakFrame(text=str(err)), FrameDirection.DOWNSTREAM)
                return True
        now = int(time.time())
        if kind == "timer":
            text = "; ".join(
                (i.get("message") or i["label"]) + ": " + i["state"]
                + (f", {max(0, i['due'] - now)} seconds remaining" if i["state"] == "scheduled" else "")
                for i in items
            )
        else:
            text = "; ".join(
                (i.get("message") or i["label"]) + ": " + i["state"]
                + (", " + datetime.fromtimestamp(i["due"], ZoneInfo(i["timezone"])).strftime("%A %d %B at %H:%M %Z")
                   if i.get("due") else "")
                for i in items
            )
        none = f"You don't have any {kind}s set" + (f" for {slots['date']}." if slots.get("date") else ".")
        await self.push_frame(TTSSpeakFrame(text=text or none), FrameDirection.DOWNSTREAM)
        return True

    async def _run_schedule_action(self, kind: str, slots: dict) -> bool:
        action = slots.get("action")
        if action not in SCHEDULE_ACTIONS:
            # cancel_all/complete have no equivalent on EchoMuseAction's MCP
            # schema -- let the LLM's own (equally limited) tool try.
            return False
        try:
            # Unfiltered: a bare "stop ringing"/"snooze it" names no kind,
            # and the active alert may be any kind.
            raw = await self._bridge.call_tool("EchoMuseList", {})
            content = json.loads(raw)
            if not isinstance(content, dict) or "snapshot" not in content:
                raise ValueError(content if isinstance(content, str) else "Unexpected scheduler response")
        except Exception as err:
            logger.warning("Local schedule: list-before-action failed, falling through to the LLM: {}", err)
            return False
        data = content["snapshot"]
        target = slots.get("target")
        if action in ("dismiss", "snooze") and not target:
            current = next((i for i in data["items"] if i["id"] == data.get("active")), None)
            if current:
                kind = current["kind"]
        try:
            item = schedule_select.select(data, kind, target, slots, recent=self._last_created,
                                           now=datetime.now(ZoneInfo(self._timezone)))
        except ValueError as err:
            await self.push_frame(TTSSpeakFrame(text=str(err)), FrameDirection.DOWNSTREAM)
            return True
        args = {"action": action, "id": item["id"]}
        if action == "snooze" and slots.get("request"):
            try:
                seconds, rest = schedule_language.duration(slots["request"] + " " + slots.get("unit", ""))
            except ValueError as err:
                await self.push_frame(TTSSpeakFrame(text=str(err)), FrameDirection.DOWNSTREAM)
                return True
            if rest:
                await self.push_frame(TTSSpeakFrame(text="Please give a snooze duration"), FrameDirection.DOWNSTREAM)
                return True
            args["seconds"] = seconds
        # A tool call may have real-world effects from here on.
        try:
            raw = await self._bridge.call_tool("EchoMuseAction", args)
            content = json.loads(raw)
            if not isinstance(content, dict) or "snapshot" not in content:
                raise ValueError(content if isinstance(content, str) else "Unexpected scheduler response")
        except Exception as err:
            logger.warning("Local schedule: action failed: {}", err)
            await self.push_frame(TTSSpeakFrame(text="Sorry, that didn't work."), FrameDirection.DOWNSTREAM)
            return True
        text = {
            "cancel": f"{kind.title()} cancelled.", "dismiss": f"{kind.title()} stopped.",
            "pause": "Timer paused.", "resume": "Timer resumed.",
            "enable": f"{kind.title()} turned on.", "disable": f"{kind.title()} turned off.",
        }.get(action, "Done.")
        if action == "snooze":
            text = "Snoozed for " + schedule_dialogue.duration_speech(
                args.get("seconds") or data.get("settings", {}).get("snooze", 540)) + "."
        await self.push_frame(TTSSpeakFrame(text=text), FrameDirection.DOWNSTREAM)
        return True
