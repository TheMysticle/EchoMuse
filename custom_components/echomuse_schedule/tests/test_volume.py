"""Added English sentences stay narrow; native explicit targets retain their slots."""
from copy import deepcopy
from pathlib import Path
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from hassil import Intents, recognize_best
from hassil.util import merge_dict
from home_assistant_intents import get_intents
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent

from custom_components.echomuse_schedule.volume import VolumeHandler


def grammars():
    native = deepcopy(get_intents("en"))
    native["lists"]["name"] = {"values":[{"in":"TV", "context":{"domain":"media_player"}}]}
    native["lists"]["area"] = {"values":["Kitchen", "Bedroom"]}
    for key in ("floor", "domain", "device_class"):
        native["lists"].setdefault(key, {"values":[]})
    merged = deepcopy(native)
    custom = yaml.safe_load((Path(__file__).resolve().parents[3]/"custom_sentences/en/echomuse_schedule.yaml").read_text())
    for definition in custom["intents"].values():
        for row in definition["data"]:
            row["metadata"] = {"custom_sentence":True}
    merge_dict(merged, custom)
    return Intents.from_dict(native), Intents.from_dict(merged)


def recognize(text, grammar):
    return recognize_best(text, grammar, language="en", best_metadata_key="custom_sentence",
        intent_context={"area":{"value":"Kitchen", "text":"Kitchen"}})


@pytest.mark.parametrize("phrase", ["volume 50%", "volume fifty percent", "volume to 50 percent",
    "the volume 50", "set volume to 50 percent", "set the volume to 0 percent",
    "change the volume to 100%", "turn volume up to 50%", "turn the volume down to 50 percent"])
def test_absolute_local_volume_sentences(phrase):
    _, grammar = grammars()
    result = recognize(phrase, grammar)
    assert result is not None and result.intent.name == "HassSetVolume"
    assert result.entities["volume_level"].value in (0, 50, 100)


@pytest.mark.parametrize("phrase", ["volume up", "volume down", "turn up the volume",
    "decrease the volume", "increase the volume by 10 percent", "volume down by 20%"])
def test_relative_local_volume_sentences(phrase):
    _, grammar = grammars()
    result = recognize(phrase, grammar)
    assert result is not None and result.intent.name == "HassSetVolumeRelative"


@pytest.mark.parametrize("phrase", [
    "set TV volume to 50 percent", "set Kitchen volume to 50 percent",
    "set the volume to 50 percent in Bedroom", "turn up TV volume",
    "volume up in Kitchen", "increase Bedroom volume by 10 percent",
    "play volume 50", "play turn up the volume", "play don't stop the music",
    "turn on TV", "stop TV", "stop", "what is the volume",
    "don't set volume to 50 percent", "do not turn up the volume",
    "set the light brightness to 50 percent", "volume 101 percent", "volume -1 percent",
])
def test_explicit_targets_and_unrelated_phrases_keep_native_matching(phrase):
    native, grammar = grammars()
    before, after = recognize(phrase, native), recognize(phrase, grammar)
    assert after is None or after.intent.name not in ("EchoMuseVolume", "EchoMuseVolumeRelative")
    assert (None if before is None else before.intent.name) == (None if after is None else after.intent.name)
    if before:
        assert {k:v.value for k,v in before.entities.items()} == {k:v.value for k,v in after.entities.items()}


@pytest.mark.parametrize("text,slots,language", [
    (None, {"volume_level":{"value":50}}, "en"),
    ("set TV volume to 50 percent", {"volume_level":{"value":50},"name":{"value":"TV"}}, "en"),
    ("set Kitchen volume to 50 percent", {"volume_level":{"value":50},"area":{"value":"Kitchen"}}, "en"),
    ("volume 50%", {"volume_level":{"value":50},"area":{"value":"Kitchen"},"domain":{"value":["media_player"]}}, "en"),
    ("Lautstärke 50 Prozent", {"volume_level":{"value":50},"area":{"value":"Kitchen"}}, "de"),
])
def test_explicit_typed_and_other_language_intent_calls_delegate_unchanged(tmp_path, text, slots, language):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        original = SimpleNamespace(intent_type="HassSetVolume", description="Volume", platforms={"media_player"},
            slot_schema=None, async_handle=AsyncMock(return_value="native"))
        handler = VolumeHandler("HassSetVolume", original, {"a":SimpleNamespace(device_id="a")})
        intent.async_register(hass, handler)
        before = deepcopy(slots)
        assert await intent.async_handle(hass, "conversation", "HassSetVolume", slots, text,
            device_id="a", language=language) == "native"
        delegated = original.async_handle.call_args.args[0]
        assert delegated.slots == before and slots == before
        original.async_handle.assert_awaited_once()
        await hass.async_stop(force=True)
    asyncio.run(run())
