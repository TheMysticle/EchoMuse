"""Gemini Live is told which languages to transcribe the user in.

Without it the transcriber guessed German and Swedish for Polish commands and
the heard check refused what Gemini had understood (2026-11-03). The server
accepts language_codes; google-genai refuses to send it, so the converter is
replaced, and anything that still refuses it must cost the setting, never the
connection.
"""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import echomuse
import transcription_languages as tl
from google.genai import _live_converters as conv
from google.genai import types


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(tl, "_refused", None)


@pytest.mark.parametrize("raw,codes", [
    ("pl-PL, en-US", ["pl-PL", "en-US"]),
    ("pl-PL,en-US,de-DE", ["pl-PL", "en-US", "de-DE"]),
    ("pl-pl; EN-us", ["pl-PL", "en-US"]),
    ("pl en", ["pl", "en"]),
    ("pl-PL, pl-PL", ["pl-PL"]),
    ("zh-hant, es-419", ["zh-Hant", "es-419"]),
    (["pl-PL", "en-US"], ["pl-PL", "en-US"]),
    ("", []), (None, []), ("  ,, ", []),
    ("Polish, pl-PL, en_US", ["pl-PL"]),
])
def test_any_number_of_languages_are_read_from_the_option(raw, codes):
    assert tl.parse(raw) == codes


def test_the_option_comes_from_the_add_on_options(tmp_path):
    p = tmp_path / "options.json"
    p.write_text(json.dumps({"log_level": "INFO", "transcription_languages": "pl-PL, en-US"}))
    assert tl.configured(str(p)) == ["pl-PL", "en-US"]
    p.write_text(json.dumps({"log_level": "INFO"}))
    assert tl.configured(str(p)) == []
    assert tl.configured(str(tmp_path / "missing.json")) == []
    p.write_text("not json")
    assert tl.configured(str(p)) == []


def setup_message(config):
    """What google-genai puts in the BidiGenerateContent setup it sends."""
    parent = {}
    conv._LiveConnectConfig_to_mldev(None, config, parent)
    return parent["setup"]


def test_the_languages_reach_the_setup_message():
    config = types.LiveConnectConfig(input_audio_transcription=types.AudioTranscriptionConfig(),
                                     output_audio_transcription=types.AudioTranscriptionConfig())
    assert tl.apply(config, ["pl-PL", "en-US"]) == ["pl-PL", "en-US"]
    msg = setup_message(config)
    assert msg["inputAudioTranscription"] == {"languageCodes": ["pl-PL", "en-US"]}
    # Only what the user says; Gemini's own speech is transcribed as before.
    assert msg["outputAudioTranscription"] == {}


def test_without_languages_nothing_changes():
    config = types.LiveConnectConfig(input_audio_transcription=types.AudioTranscriptionConfig())
    assert tl.apply(config, []) == []
    assert setup_message(config)["inputAudioTranscription"] == {}


def test_a_refusal_naming_the_languages_turns_them_off_for_later_connects():
    config = types.LiveConnectConfig(input_audio_transcription=types.AudioTranscriptionConfig())
    tl.apply(config, ["pl-PL"])
    assert tl.failed(config, RuntimeError("1007 Invalid JSON payload: unknown name languageCodes"))
    again = types.LiveConnectConfig(input_audio_transcription=types.AudioTranscriptionConfig())
    assert tl.apply(again, ["pl-PL"]) == []
    assert setup_message(again)["inputAudioTranscription"] == {}


def test_an_unrelated_failure_keeps_the_languages():
    config = types.LiveConnectConfig(input_audio_transcription=types.AudioTranscriptionConfig())
    tl.apply(config, ["pl-PL"])
    assert not tl.failed(config, RuntimeError("1008 The operation was aborted."))
    assert tl.apply(config, ["pl-PL"]) == ["pl-PL"]


def test_a_connect_refused_over_the_languages_reconnects_without_them(monkeypatch):
    monkeypatch.setattr(tl, "configured", lambda path=tl.OPTIONS_PATH: ["pl-PL", "en-US"])
    live = SimpleNamespace(_disconnecting=False, _session_resumption_handle="h",
                           _handle_connection_error=AsyncMock(return_value=True),
                           _reconnect=AsyncMock(), create_task=lambda coro, name: coro.close())
    seen = []

    async def connect(config):
        seen.append(config.input_audio_transcription.language_codes)
        if config.input_audio_transcription.language_codes:
            raise ValueError("language_codes parameter is not supported in Gemini API.")

    async def scenario():
        for _ in range(2):
            config = types.LiveConnectConfig(input_audio_transcription=types.AudioTranscriptionConfig())
            await echomuse.keep_connection(live, connect, config)

    asyncio.run(scenario())
    assert seen == [["pl-PL", "en-US"], None]
    live._handle_connection_error.assert_awaited_once()


def test_two_languages_make_the_reply_follow_the_speaker():
    rule = tl.reply_rule("en", ["pl-PL", "en-US"])
    assert "Polish and English" in rule
    assert "latest turn, however short" in rule
    assert "a command in Polish gets its answer in Polish" in rule
    assert "If you cannot tell, reply in English" in rule


def test_three_languages_are_all_named():
    assert "Polish, English and German" in tl.reply_rule("pl", ["pl-PL", "en-US", "de-DE"])


def test_one_language_or_none_keeps_the_single_language_rule():
    assert tl.reply_rule("en", ["pl-PL"]) is None
    assert tl.reply_rule("en", ["en-US", "en-GB"]) is None
    assert tl.reply_rule("en", []) is None


def test_the_fallback_is_a_listed_language():
    assert "If you cannot tell, reply in Polish" in tl.reply_rule("fr", ["pl-PL", "en-US"])


def test_the_composed_prompt_uses_it(monkeypatch):
    monkeypatch.setattr(tl, "configured", lambda path=tl.OPTIONS_PATH: ["pl-PL", "en-US"])
    text = echomuse.composed_instructions({}, "en")
    assert "The household speaks Polish and English" in text
    assert "Do not switch for accents, short speech" not in text
    monkeypatch.setattr(tl, "configured", lambda path=tl.OPTIONS_PATH: [])
    assert "Do not switch for accents, short speech" in echomuse.composed_instructions({}, "en")
