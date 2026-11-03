"""Tell Gemini Live which languages the user speaks, for its input transcript.

Without this the transcriber guesses the language of every turn, and on short
Polish commands it guesses wrong: "Włącz lampę podłogową" came back as
"Wunschlampe podłogową" (German) and once as "Bortglömda på Dogo." (Swedish),
and the heard check, which reads that transcript, refused a command Gemini had
understood (2026-11-03). Measured the same day against
gemini-3.1-flash-live-preview, the same audio twice each:

    "Włącz lampę podłogową"   none: "wontschlampę podłogową"  pl-PL,en-US: correct
    "Włącz telewizor"         none: "Wohnstelle wie soll?"    pl-PL,en-US: correct
    "Ustaw budzik na siódmą"  none: Japanese kana             pl-PL,en-US: Polish
    English commands          correct either way

The setting is `language_codes` on the input AudioTranscriptionConfig, and any
number of languages may be given. google-genai refuses it for the Gemini API
("language_codes parameter is not supported in Gemini API") in the converter
that serialises the setup message, but the server accepts it, and the table
above was measured by sending it directly. So that converter is replaced here
with one that sends it. If anything still refuses it - a later SDK, or the
server - the session reconnects without it and says why in the log: a voice
assistant that hears worse is better than one that cannot connect.
"""
from __future__ import annotations

import json
import re

from loguru import logger

OPTIONS_PATH = "/data/options.json"
OPTION = "transcription_languages"

# BCP-47 as Gemini takes it: a language, optionally a region or script.
_CODE = re.compile(r"^([a-z]{2,3})(?:-([a-z0-9]{2,4}))?$", re.I)

# Once refused, never sent again by this process.
_refused: str | None = None


def parse(raw) -> list[str]:
    """`"pl-PL, en-US"` -> `["pl-PL", "en-US"]`: commas, semicolons or spaces
    between codes, case normalised, duplicates and malformed codes dropped."""
    if isinstance(raw, (list, tuple)):
        raw = ",".join(str(x) for x in raw)
    out: list[str] = []
    for part in re.split(r"[,;\s]+", str(raw or "")):
        if not part:
            continue
        m = _CODE.match(part)
        if not m:
            logger.warning("Transcription language {!r} is not a language code such as pl-PL; ignored", part)
            continue
        lang, region = m.group(1).lower(), m.group(2)
        code = lang + (f"-{region.upper() if len(region) == 2 else region.title()}" if region else "")
        if code not in out:
            out.append(code)
    return out


# English names for the reply rule; a code not listed is given as the code.
NAMES = {
    "pl": "Polish", "en": "English", "de": "German", "fr": "French", "es": "Spanish", "it": "Italian",
    "pt": "Portuguese", "nl": "Dutch", "sv": "Swedish", "no": "Norwegian", "nb": "Norwegian", "da": "Danish",
    "fi": "Finnish", "cs": "Czech", "sk": "Slovak", "uk": "Ukrainian", "ru": "Russian", "lt": "Lithuanian",
    "lv": "Latvian", "et": "Estonian", "hu": "Hungarian", "ro": "Romanian", "bg": "Bulgarian", "hr": "Croatian",
    "sr": "Serbian", "sl": "Slovenian", "el": "Greek", "tr": "Turkish", "ja": "Japanese", "zh": "Chinese",
    "ko": "Korean", "hi": "Hindi", "ar": "Arabic", "he": "Hebrew", "vi": "Vietnamese", "th": "Thai",
    "id": "Indonesian",
}


def reply_rule(language: str | None, codes: list[str] | None = None) -> str | None:
    """The reply-language instruction when the household speaks more than one
    language; None leaves the add-on's single-language rule in place.

    The native-audio Live models choose their reply language themselves and
    take no language setting; only the instructions steer it. The add-on's
    own rule says to keep the configured language and not to switch for
    "very short speech", which is every voice command, so Polish commands
    were answered in English (2026-11-03). With the languages listed here the
    transcript is reliable, so the rule can follow the speaker instead.
    """
    codes = configured() if codes is None else codes
    langs = list(dict.fromkeys(c.split("-")[0] for c in codes))
    if len(langs) < 2:
        return None
    names = [NAMES.get(l, l) for l in langs]
    base = (language or "").split("-")[0].lower()
    fallback = NAMES.get(base, base) if base in langs else names[0]
    spoken = ", ".join(names[:-1]) + " and " + names[-1]
    return (f" The household speaks {spoken}. Reply in whichever of these the user spoke in their latest turn, "
            f"however short it was: a command in {names[0]} gets its answer in {names[0]}. If you cannot tell, "
            f"reply in {fallback}. Never reply in a language not listed here. Names or titles in another language "
            f"in tool results or the home setup do not change your reply language.")


def configured(path: str = OPTIONS_PATH) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return parse(json.load(f).get(OPTION, ""))
    except (OSError, ValueError, AttributeError):
        return []


def allow_in_sdk() -> bool:
    """Let google-genai send language_codes to the Gemini API."""
    try:
        from google.genai import _live_converters as conv
    except ImportError:
        return False
    if getattr(conv, "_em_language_codes", False):
        return True
    original = getattr(conv, "_AudioTranscriptionConfig_to_mldev", None)
    getv = getattr(conv, "getv", None)
    if original is None or getv is None:
        return False

    def to_mldev(from_object, parent_object=None):
        codes = getv(from_object, ["language_codes"])
        if codes is None:
            return original(from_object, parent_object)
        if isinstance(from_object, dict):
            rest = {k: v for k, v in from_object.items() if k != "language_codes"}
        else:
            rest = from_object.model_copy(update={"language_codes": None})
        out = original(rest, parent_object)
        out["languageCodes"] = list(codes)
        return out

    conv._AudioTranscriptionConfig_to_mldev = to_mldev
    conv._em_language_codes = True
    return True


def apply(config, codes: list[str] | None = None) -> list[str]:
    """Put the configured languages on a LiveConnectConfig's input transcription.
    Returns what was set, [] when nothing was."""
    codes = configured() if codes is None else codes
    if not codes or _refused:
        return []
    if not allow_in_sdk():
        refuse("this google-genai cannot send it")
        return []
    from google.genai.types import AudioTranscriptionConfig
    config.input_audio_transcription = AudioTranscriptionConfig(language_codes=codes)
    return codes


def refuse(why: str) -> None:
    global _refused
    if not _refused:
        _refused = why
        logger.warning("Transcription languages turned off for this add-on run: {}", why)


def failed(config, error: BaseException) -> bool:
    """After a connection failed: if the languages are why, stop sending them.
    True when it did, so the reconnect that follows goes without them."""
    tr = getattr(config, "input_audio_transcription", None)
    if not getattr(tr, "language_codes", None):
        return False
    text = str(error)
    if "language" not in text.lower():
        return False
    refuse(text[:200])
    return True
