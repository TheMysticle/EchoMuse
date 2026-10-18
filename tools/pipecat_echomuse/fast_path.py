"""Local sentence/intent fast-path: skip the LLM entirely for a narrow, exact
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
and an ambiguous reference ("turn it off", ambiguous area/name) should fall
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

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import home_assistant_intents
import yaml
from hassil import Intents, RecognizeResult, TextSlotList, errors, recognize
from hassil.util import merge_dict
from loguru import logger

from pipecat.frames.frames import Frame, TranscriptionFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

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
    merged = dict(data)
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
                    merge_dict(merged, custom)
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
            fast_path_eligible=name in FAST_PATH_INTENTS,
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


def match(state: FastPathState, text: str, language: str) -> RecognizeResult | None:
    """Try to recognize `text` as one of the allowed fast-path intents.

    Returns None on no match, an ambiguous/unmatched entity, or an intent
    outside FAST_PATH_INTENTS -- all of which fall through to the LLM.
    """

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
    if result.intent.name not in FAST_PATH_INTENTS:
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
        args[slot_name] = entity.value
    return args


def build_confirmation(intent_name: str, args: dict[str, Any], tool_result: dict[str, Any]) -> str | None:
    """Build a short spoken confirmation from an HA intent tool's structured
    result, or None if the result shape is not one this can safely confirm
    (in which case the caller must fall through to the LLM instead of
    guessing at what happened)."""

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
    """Intercepts a finalized transcript, and on a clean match against
    FAST_PATH_INTENTS, calls the matching HA MCP tool directly and speaks a
    deterministic confirmation -- never reaching the LLM.

    Safety property: once a tool call is attempted, this step is ALWAYS
    terminal for the turn (the frame is absorbed, never also forwarded to
    the LLM) even if the call or the confirmation-building fails, because a
    real-world action may already have happened; forwarding on to the LLM
    at that point risks the same command being acted on twice.
    """

    def __init__(
        self,
        *,
        state: FastPathState,
        language: str,
        bridge: Any,
        tool_names: dict[str, str],
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._state = state
        self._language = language
        self._bridge = bridge
        self._tool_names = tool_names

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            if await self._try_fast_path(frame.text.strip()):
                return  # absorbed -- never reaches the LLM

        await self.push_frame(frame, direction)

    async def _try_fast_path(self, text: str) -> bool:
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
            # The call itself raised -- connection/HA error, nothing done.
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
            except (json.JSONDecodeError, TypeError):
                confirmation = None
            if confirmation is None:
                confirmation = f"{_VERBS.get(result.intent.name, 'Done')}."

        logger.info("Fast-path hit: {!r} -> {} ({}) args={}", text, result.intent.name, tool_name, args)
        await self.push_frame(TTSSpeakFrame(text=confirmation), FrameDirection.DOWNSTREAM)
        return True
