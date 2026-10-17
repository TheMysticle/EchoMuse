"""Playback modes preserve native targeting and do not start/change queues."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from hassil import recognize_best
from homeassistant.components.media_player import RepeatMode
from homeassistant.exceptions import HomeAssistantError

from custom_components.echomuse_spotify.api import SpotifyApi
from test_voice import grammar, player


@pytest.mark.parametrize("phrase,mode,value", [
    ("shuffle", "shuffle", True), ("shuffle on", "shuffle", True),
    ("shuffle music", "shuffle", True), ("shuffle this playlist", "shuffle", True),
    ("turn on shuffle", "shuffle", True), ("shuffle off", "shuffle", False),
    ("turn shuffle off", "shuffle", False), ("stop shuffling", "shuffle", False),
    ("enable shuffle on Studio speaker", "shuffle", True),
    ("disable shuffle in the Kitchen", "shuffle", False),
    ("repeat this song", "repeat", "one"), ("repeat the current track", "repeat", "one"),
    ("repeat the playlist", "repeat", "all"), ("repeat all", "repeat", "all"),
    ("turn off repeat", "repeat", "off"),
    ("repeat this track on Studio speaker", "repeat", "one"),
    ("repeat this album in the Kitchen", "repeat", "all"),
])
def test_exact_mode_sentences(grammar, phrase, mode, value):
    result = recognize_best(phrase, grammar, language="en", best_metadata_key="custom_sentence",
        intent_context={"area": {"value": "Kitchen", "text": "Kitchen"}})
    assert result is not None
    assert result.intent.name == ("EchoMuseSpotifyShuffle" if mode == "shuffle" else "EchoMuseSpotifyRepeat")
    assert result.entities[mode].value == value


@pytest.mark.parametrize("phrase", [
    "repeat", "repeat that", "repeat after me", "repeat what you just said",
    "play Repeat on Studio speaker", "play Shuffle on Studio speaker",
    "turn on Desk lamp", "stop", "stop the music", "stop Cleaner",
])
def test_mode_sentences_do_not_capture_speech_repeat_titles_or_other_commands(grammar, phrase):
    result = recognize_best(phrase, grammar, language="en", best_metadata_key="custom_sentence",
        intent_context={"area": {"value": "Kitchen", "text": "Kitchen"}})
    assert result is None or result.intent.name not in ("EchoMuseSpotifyShuffle", "EchoMuseSpotifyRepeat")


@pytest.mark.parametrize("value", [False, True])
def test_shuffle_api_targets_only_requested_connect_device(value):
    async def run():
        api = SpotifyApi(None, None)
        api._request = AsyncMock()
        await api.set_shuffle("speaker", value)
        api._request.assert_awaited_once_with("PUT", "/me/player/shuffle",
            params={"device_id": "speaker", "state": str(value).lower()})
    asyncio.run(run())


@pytest.mark.parametrize("value", ["off", "context", "track"])
def test_repeat_api_uses_spotify_modes_and_explicit_device(value):
    async def run():
        api = SpotifyApi(None, None)
        api._request = AsyncMock()
        await api.set_repeat("speaker", value)
        api._request.assert_awaited_once_with("PUT", "/me/player/repeat",
            params={"device_id": "speaker", "state": value})
    asyncio.run(run())


@pytest.mark.parametrize("mode,value", [("shuffle", "false"), ("shuffle", 1), ("repeat", "all"), ("repeat", "invalid")])
def test_bad_modes_are_rejected_before_network_calls(mode, value):
    async def run():
        api = SpotifyApi(None, None)
        api._request = AsyncMock()
        with pytest.raises(HomeAssistantError):
            await (api.set_shuffle("speaker", value) if mode == "shuffle" else api.set_repeat("speaker", value))
        api._request.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("repeat,spotify", [(RepeatMode.ONE, "track"), (RepeatMode.ALL, "context"), (RepeatMode.OFF, "off")])
def test_repeat_controls_preserve_queue_and_map_native_ha_values(repeat, spotify):
    async def run():
        entity, api = player()
        api.set_repeat = AsyncMock()
        await entity.async_set_repeat(repeat)
        api.set_repeat.assert_awaited_once_with("speaker", spotify)
        api.transfer.assert_not_called()
        api.play_uri.assert_not_called()
        api.command.assert_not_called()
    asyncio.run(run())


def test_shuffle_does_not_start_playback_or_transfer():
    async def run():
        entity, api = player()
        api.set_shuffle = AsyncMock()
        await entity.async_set_shuffle(False)
        api.set_shuffle.assert_awaited_once_with("speaker", False)
        api.transfer.assert_not_called()
        api.play_uri.assert_not_called()
        api.command.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("state,expected", [("off", RepeatMode.OFF), ("track", RepeatMode.ONE), ("context", RepeatMode.ALL), ("unknown", None)])
def test_modes_only_describe_this_devices_active_playback(active, state, expected):
    entity, _ = player(active)
    entity.coordinator.data.playback.update(shuffle_state=True, repeat_state=state)
    assert entity.shuffle is (True if active else None)
    assert entity.repeat == (expected if active else None)
