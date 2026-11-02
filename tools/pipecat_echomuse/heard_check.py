"""Refuse a Gemini Live tool call whose target the user never said.

Gemini Live picks tools from the audio, and its transcript of that audio is a
separate stream. On a false wake or a misheard command it acts on what it
guessed: "internal kitchen master" ran the kitchen vacuum script, Polish
chatter paused and turned off the TV, "Komm du jetzt in Salon ja?" sent the
vacuum to the corridor (all from the 2026-10-26..28 add-on logs). This checks
each acting call against the transcript of the turn and refuses one whose
target, or whose request, was not said. It never chooses an action, so it
cannot cause a wrong one; at worst a right one is refused and is repeated.

**It costs no latency, measured rather than assumed** (2026-10-28, 54 replayed
sessions against gemini-3.1-flash-live-preview with the add-on's own prompt
and tools, chained commands included): Gemini's input transcript was complete
150-550 ms after activity_end, and its tool calls came 355-713 ms after THAT,
a chain's calls together in one batch. In no session did a call arrive before
the words that justify it. `HeardState.evidence` still waits briefly for a
transcript that has not come, so a slower day degrades to a short wait rather
than a refusal.

Measured against the same sessions and the logged history: 37 of 38 real
commands pass (the one refused named a light Gemini invented), 22 of 24 junk
calls are refused. What words cannot catch is speech that really is a request,
made to someone else: "Mamo, posprzątasz potem kuchnię?" passes for the kitchen
vacuum script. Proactive audio, Gemini's own "was this for me" switch, was
measured too and made it worse on this model (it acted on more junk), which is
why this is done here rather than left to the model.

The check itself is pure text: `check()` has no I/O, model or clock.
"""
from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import time
import unicodedata

from loguru import logger

# Tools that only read. A misheard read costs a wrong answer, not a vacuum run.
READ_ONLY = re.compile(
    r"(GetLiveContext|GetDateTime|EchoMuseList|search_media|RequestContinuation|get_weather|web_search)$")

# Words that carry no target. Kept small: a word here can never be evidence.
GENERIC = {
    "the", "a", "an", "to", "of", "on", "off", "in", "at", "up", "down", "my", "your",
    "turn", "toggle", "power", "activate", "set", "please", "switch", "go", "and",
}

# Polish stems -> the English words Home Assistant names use. Matched as
# prefixes of the de-accented transcript word, so inflection does not matter.
STEMS = {
    "kuchn": "kitchen", "salon": "living room", "pokoj": "room", "sypial": "bedroom",
    "lazienk": "bathroom", "korytarz": "corridor", "przedpok": "corridor",
    "swiatl": "light lights", "swiatel": "light lights", "lamp": "lamp light",
    "stojac": "floor standing", "telewiz": "tv", "odkurz": "vacuum", "sprzat": "clean",
    "posprzat": "clean", "komputer": "computer pc", "film": "movie", "kino": "movie",
    "noc": "night", "nocn": "night", "pogod": "weather", "klimatyz": "ac climate",
    "termostat": "thermostat", "nawilz": "humidifier", "budzik": "alarm", "minutnik": "timer",
    "przypom": "reminder", "glosn": "volume", "ciszej": "volume", "pauz": "pause",
    "zatrzym": "stop pause", "nastepn": "next", "poprzedn": "previous", "wznow": "resume play",
    "graj": "play", "zagraj": "play", "pusc": "play", "muzyk": "music", "przestan": "stop",
    "wystarcz": "stop", "wycisz": "mute", "nagryw": "recording", "przyciem": "dim",
    "rozjasn": "bright", "jasn": "bright", "procent": "percent", "kolor": "colour",
    "czerwon": "red", "niebiesk": "blue", "zielon": "green", "zolt": "yellow", "bial": "white",
    "ciepl": "warm", "zimn": "cold",
}

# What a call must be about, by tool family, when the tool alone does not say.
INTENT_WORDS = {
    "stop": "stop pause cancel enough quiet shut dismiss snooze",
    "media": "pause stop next skip previous back resume continue play music song track volume louder quieter mute unmute",
    "volume": "volume louder quieter loud quiet up down mute percent",
    # A time alone is not a request for an alarm: "wyłączyłabym go po 10
    # minutach", said of a film, set a 10-minute timer.
    "schedule": "alarm alarms timer timers reminder reminders remind wake snooze",
    "light": "light lights lamp lamps brightness bright brighter dim dimmer percent colour color "
             "red green blue yellow white warm cold cool purple pink orange",
    "climate": "temperature degrees heat heating cool cooling ac warm cold thermostat humidity",
    "vacuum": "vacuum clean hoover robot dock base",
    "broadcast": "broadcast announce tell everyone",
}

# HassTurnOn/Off act on whatever matches, an area alone included, so the
# request itself must have been said: "Kommst du jetzt in Salon" names the
# living room in Polish and asks nothing of it.
ON_OFF = set("""on off turn switch enable disable activate deactivate open close start stop
lock unlock shut kill power wlacz wylacz zapal zgas otworz zamknij uruchom odpal zatrzymaj
wlaczyc wylaczyc zgasic zapalic""".split())

# Words that ask for music, after STEMS: "zagraj" and "pusc" arrive here as play.
PLAY_WORDS = {"play", "listen", "music", "song"}

# A number is a light level, volume or temperature. Not a schedule (above).
NUMBER = re.compile(r"\d")
NUMBER_FAMILIES = ("volume", "light", "climate")


def fold(text: str) -> str:
    text = text.lower().replace("ł", "l")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", fold(text))


def heard_words(transcript: str) -> set[str]:
    """The transcript's words plus the English each Polish stem stands for."""
    out = set()
    for w in words(transcript):
        out.add(w)
        for stem, en in STEMS.items():
            if w.startswith(stem):
                out.update(en.split())
    return out


def _one_edit(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    if len(a) > len(b):
        a, b = b, a
    return any(a == b[:i] + b[i + 1:] for i in range(len(b)))


def word_heard(word: str, heard: set[str]) -> bool:
    if word in heard:
        return True
    for h in heard:
        n = min(len(word), len(h))
        # inflection: kitchen/kitchens, lights/light
        if n >= 4 and h[:max(4, n - 2)] == word[:max(4, n - 2)] and abs(len(h) - len(word)) <= 3:
            return True
        # one slip in a longer word: komputer/computer
        if len(word) >= 5 and _one_edit(word, h):
            return True
    return False


class Heard(set):
    """heard_words(), plus the transcript run together, for names whose
    spacing the two sides disagree on: "kitchen W LED" against "kitchen WLED"."""

    def __init__(self, transcript: str):
        super().__init__(heard_words(transcript))
        self.joined = "".join(words(transcript))


def phrase_heard(phrase: str, heard: set[str]) -> bool:
    toks = [w for w in dict.fromkeys(words(phrase)) if w not in GENERIC]
    if not toks:
        return False
    if all(word_heard(w, heard) for w in toks):
        return True
    joined = getattr(heard, "joined", "")
    return len("".join(toks)) >= 5 and "".join(toks) in joined


class Names:
    """Every name an entity, script or area goes by in Home Assistant.

    Lets "Kitchen Lights" stand for a call naming Kitchen Master. Without it a
    call is checked against the name Gemini passed, which is what it heard in
    nearly every logged case; the aliases only widen what passes.
    """

    def __init__(self, groups=(), areas=(), scripts=None):
        self.by_phrase: dict[str, list[str]] = {}
        for names in groups:
            for n in names:
                self.by_phrase.setdefault(" ".join(words(n)), list(names))
        self.areas: dict[str, list[str]] = {}
        for names in areas:
            for n in names:
                self.areas[" ".join(words(n))] = list(names)
        self.scripts: dict[str, list[str]] = dict(scripts or {})

    def entity(self, name: str) -> list[str]:
        return self.by_phrase.get(" ".join(words(name)), [name])

    def area(self, name: str) -> list[str]:
        return self.areas.get(" ".join(words(name)), [name])


def family(tool: str) -> str | None:
    t = tool.rsplit("__", 1)[-1]
    if t == "EchoMuseStop":
        return "stop"
    if t == "EchoMuseVolume":
        return "volume"
    if t.startswith("EchoMuse") or "Timer" in t:
        return "schedule"
    if t.startswith(("HassMedia", "HassSetVolume", "play_media")):
        return "media"
    if t.startswith("HassLight"):
        return "light"
    if t.startswith(("HassClimate", "HassHumidifier")):
        return "climate"
    if t.startswith("HassVacuum"):
        return "vacuum"
    if t == "HassBroadcast":
        return "broadcast"
    return None


def check(tool: str, args: dict, transcript: str, names: Names | None = None) -> tuple[bool, str]:
    """(allowed, reason) for one call, given what the user said this turn."""
    names = names or Names()
    if READ_ONLY.search(tool):
        return True, "read-only"
    heard = Heard(transcript)
    if not heard:
        return False, "nothing was transcribed"
    short = tool.rsplit("__", 1)[-1]
    if "script__" in tool:
        slug = tool.split("script__", 1)[1]
        cands = names.scripts.get(slug) or [slug.replace("_", " ")]
        if any(phrase_heard(n, heard) for n in cands):
            return True, "script name heard"
        return False, f"none of {cands} was said"
    fam = family(tool)
    if short in ("HassTurnOn", "HassTurnOff") and not (set(words(transcript)) & ON_OFF):
        return False, "no on/off request was said"
    # A device heard is not a request to it: "Virgil TV Paul" paused the TV and
    # "Kommst du jetzt in Salon ja?" set a light called "Salon ja".
    if fam in ("media", "light") and short != "play_media":
        if not (heard & set(INTENT_WORDS[fam].split()) or (fam == "light" and NUMBER.search(transcript))):
            return False, f"no {fam} request was said"
    name = args.get("name")
    if isinstance(name, str) and name.strip():
        cands = names.entity(name)
        if any(phrase_heard(n, heard) for n in cands):
            return True, f"'{name}' heard"
        return False, f"'{name}' was not said"
    if short == "play_media":
        # media_content_id is what HA's tool sends; the other two are older shapes.
        q = str(args.get("media_content_id") or args.get("search_query") or args.get("media_id") or "")
        # Gemini's title need not match its transcript ("2010 hits" for "Twenty
        # Ten hits"), and working out what a title means is the Spotify
        # integration's search, so here only the request has to have been said.
        if q and heard & PLAY_WORDS:
            return True, "play request heard"
        return (True, "query heard") if phrase_heard(q, heard) else (False, f"'{q}' was not said")
    if fam:
        if heard & set(INTENT_WORDS[fam].split()) or (fam in NUMBER_FAMILIES and NUMBER.search(transcript)):
            return True, f"{fam} request heard"
    area = args.get("area")
    if isinstance(area, str) and area.strip():
        if any(phrase_heard(n, heard) for n in names.area(area)):
            return True, f"area '{area}' heard"
    for key in ("domain", "device_class"):
        vals = args.get(key) or []
        vals = [vals] if isinstance(vals, str) else vals
        if any(word_heard(str(v), heard) or (v == "light" and heard & {"lights", "lamp", "lamps"}) for v in vals):
            return True, f"{key} heard"
    if fam:
        return False, f"no {fam} words were said"
    return False, "no target was said"


# --- names from Home Assistant -------------------------------------------

_SCRIPT_ALIASES = re.compile(r"Aliases:\s*(\[.*\])")


def script_names(tools) -> dict[str, list[str]]:
    """Script aliases, read from the MCP tool descriptions HA already sends."""
    import ast
    out = {}
    for t in tools:
        name = getattr(t, "name", "")
        if "script__" not in name:
            continue
        slug = name.split("script__", 1)[1]
        m = _SCRIPT_ALIASES.search(getattr(t, "description", "") or "")
        aliases = []
        if m:
            try:
                aliases = [a for a in ast.literal_eval(m.group(1)) if isinstance(a, str)]
            except (ValueError, SyntaxError):
                pass
        out[slug] = [slug.replace("_", " "), *aliases]
    return out


def live_context_groups(result_text: str) -> tuple[list[list[str]], list[list[str]]]:
    """(entity name groups, area name groups) from a GetLiveContext result.

    Same input as fast_path.parse_live_context, but keeps each entity's names
    together: an alias is only evidence for the entity it belongs to.
    """
    import yaml
    groups, areas = [], []
    try:
        outer = json.loads(result_text)
        text = outer.get("result", "") if isinstance(outer, dict) else ""
        _, _, list_text = text.partition("\n- ")
        records = yaml.safe_load("- " + list_text) if list_text else []
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            for key, into in (("names", groups), ("areas", areas)):
                raw = record.get(key)
                parts = raw.split(",") if isinstance(raw, str) else raw if isinstance(raw, list) else []
                parts = [str(p).strip() for p in parts if str(p).strip()]
                if parts and parts not in into:
                    into.append(parts)
    except Exception as err:
        logger.warning("Heard check: could not read live context names: {}", err)
    return groups, areas


_NAMES_TTL = 300.0
_names_cache: dict = {"at": 0.0, "groups": [], "areas": []}


async def load_names(state: "HeardState", bridge, tools) -> None:
    """Fill `state.names`, refreshing HA's names at most every five minutes."""
    scripts = script_names(tools)
    state.names = Names(_names_cache["groups"], _names_cache["areas"], scripts)
    if time.monotonic() - _names_cache["at"] < _NAMES_TTL:
        return
    tool = next((t.name for t in tools if t.name.endswith("GetLiveContext")), None)
    if not tool:
        return
    try:
        groups, areas = live_context_groups(await bridge.call_tool(tool, {}))
    except Exception as err:
        logger.warning("Heard check: GetLiveContext failed: {}", err)
        return
    _names_cache.update(at=time.monotonic(), groups=groups, areas=areas)
    state.names = Names(groups, areas, scripts)
    logger.info("Heard check: {} entity name groups, {} areas, {} scripts", len(groups), len(areas), len(scripts))


# --- the per-session state the Gemini Live service carries ----------------

# A clarification's answer ("yes", "the kitchen one") is checked with the
# question's turn included, for this long after it. A chain of questions keeps
# every turn of it: "set an alarm" / when? / "10am" / which day? / "tomorrow"
# was refused when only the last turn before the answer counted (2026-11-02).
FOLLOW_UP_SECONDS = 30.0
# How long a call waits for a transcript that has not arrived at all. Never
# reached in the 54 measured sessions.
EVIDENCE_WAIT_SECONDS = 0.5


class HeardState:
    def __init__(self):
        self.turn = ""
        self.previous = ""
        self.previous_at = 0.0
        self.bot_since_user = False
        self.bot_text = ""
        self.asked = False
        self.names = Names()

    def question_asked(self) -> None:
        """The model asked for a clarification (EchoMuseRequestContinuation).

        Its transcript need not end in "?": the tool is the surer sign.
        """
        self.asked = True

    def user_text(self, text: str) -> None:
        if self.bot_since_user:
            asked = self.asked or self.bot_text.rstrip().endswith("?")
            if asked:
                now = time.monotonic()
                chain = self.previous if now - self.previous_at < FOLLOW_UP_SECONDS else ""
                self.previous = (chain + " " + self.turn).strip()
                self.previous_at = now
            else:
                self.previous, self.previous_at = "", 0.0
            self.turn, self.bot_text, self.bot_since_user, self.asked = "", "", False, False
        self.turn += text

    def bot_text_out(self, text: str) -> None:
        self.bot_since_user = True
        self.bot_text += text

    def transcript(self) -> str:
        if self.previous and time.monotonic() - self.previous_at < FOLLOW_UP_SECONDS:
            return self.previous + " " + self.turn
        return self.turn

    async def evidence(self) -> str:
        deadline = time.monotonic() + EVIDENCE_WAIT_SECONDS
        while not self.turn.strip() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        return self.transcript()


def state_of(service) -> HeardState:
    state = getattr(service, "_em_heard", None)
    if state is None:
        state = service._em_heard = HeardState()
    return state


REFUSAL = ("Not done: the user's words did not ask for this ({why}). Do not retry it and do "
           "not try another tool for it. If the user was talking to you, say only that you did "
           "not catch that; otherwise say nothing.")


def gated(service, handler):
    """Wrap a registered tool handler so an unheard call is refused.

    ECHOMUSE_HEARD_CHECK=off in the add-on's environment turns it off.
    """
    if handler is None or os.environ.get("ECHOMUSE_HEARD_CHECK", "").lower() == "off":
        return handler

    @functools.wraps(handler)
    async def gate(params):
        tool = params.function_name or ""
        if tool == "EchoMuseRequestContinuation":
            state_of(service).question_asked()
        if READ_ONLY.search(tool):
            return await handler(params)
        state = state_of(service)
        transcript = await state.evidence()
        ok, why = check(tool, dict(params.arguments or {}), transcript, state.names)
        if ok:
            return await handler(params)
        logger.info("Heard check refused {} {}: {} (heard {!r})",
                    tool, dict(params.arguments or {}), why, transcript)
        await params.result_callback({"error": "not_heard", "message": REFUSAL.format(why=why)})

    return gate
