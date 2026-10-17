"""Real HA sentence parsing and player methods, with only Spotify simulated."""
import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError
import yaml
from hassil import Intents, recognize_best
from hassil.util import merge_dict
from home_assistant_intents import get_intents
from homeassistant.components.media_player import MediaType

from custom_components.echomuse_spotify.coordinator import AccountState
from custom_components.echomuse_spotify.media_player import SpotifyConnectPlayer

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def grammar():
    definitions = deepcopy(get_intents("en"))
    custom = yaml.safe_load((ROOT / "custom_sentences/en/echomuse_spotify.yaml").read_text())
    for intent_definition in custom["intents"].values():
        for row in intent_definition["data"]:
            row["metadata"] = {"custom_sentence": True}
    merge_dict(definitions, custom)
    definitions["lists"]["name"] = {"values": [{"in": "Studio speaker", "context": {"domain": "media_player"}}]}
    definitions["lists"]["area"] = {"values": ["Kitchen"]}
    for key in ("floor", "domain", "device_class"):
        definitions["lists"].setdefault(key, {"values": []})
    return Intents.from_dict(definitions)


@pytest.mark.parametrize("phrase", [
    "play", "continue", "play music", "play some music", "play the music",
    "put on music", "start music", "continue playing music",
    "play music on Studio speaker", "play some music in the Kitchen",
    "resume music", "pause music", "next track", "previous track",
])
def test_generic_playback_uses_native_transport_intents(grammar, phrase):
    result = recognize_best(phrase, grammar, language="en", best_metadata_key="custom_sentence",
        intent_context={"area": {"value": "Kitchen", "text": "Kitchen"}})
    assert result is not None
    expected = "HassMediaPause" if phrase.startswith("pause") else "HassMediaNext" if phrase == "next track" else "HassMediaPrevious" if phrase == "previous track" else "HassMediaUnpause"
    assert result.intent.name == expected
    assert "search_query" not in result.entities


@pytest.mark.parametrize("phrase", [
    "play jazz", "play Music by Madonna", "play the artist Music",
    "play the track Music", "play the album Music", "play Music Sounds Better With You",
    "play stop", "play alarm", "play Beauty on the Beat on Studio speaker",
    "play please don't stop the music", "play don't stop the music",
    "play Please Dont Stop the Music on Studio speaker",
    "play the track Please Don't Stop the Music",
])
def test_named_music_delegates_with_the_same_slots_as_native_search(grammar, phrase):
    result = recognize_best(phrase, grammar, language="en", best_metadata_key="custom_sentence",
        intent_context={"area": {"value": "Kitchen", "text": "Kitchen"}})
    assert result is not None and result.intent.name == "EchoMuseSpotifySearchAndPlay"
    # Compare actual recognized title/type/target slots, not just the name
    # of the adapter. A wildcard swallowing 'artist' would change behavior.
    # Reuse a grammar with only the two added intents removed.
    native = deepcopy(grammar)
    native.intents.pop("EchoMuseSpotifySearchAndPlay")
    for row in native.intents["HassMediaUnpause"].data:
        if row.metadata:
            row.metadata.pop("custom_sentence", None)
    baseline = recognize_best(phrase, native, language="en",
        intent_context={"area": {"value": "Kitchen", "text": "Kitchen"}})
    assert baseline is not None and baseline.intent.name == "HassMediaSearchAndPlay"
    assert {name: entity.value for name, entity in result.entities.items()} == {
        name: entity.value for name, entity in baseline.entities.items()}


@pytest.mark.parametrize("phrase", ["turn on the music lights", "stop Cleaner", "arm the alarm", "start a timer", "please don't stop the music", "dont stop the music"])
def test_resume_aliases_do_not_capture_other_commands(grammar, phrase):
    result = recognize_best(phrase, grammar, language="en", best_metadata_key="custom_sentence",
        intent_context={"area": {"value": "Kitchen", "text": "Kitchen"}})
    assert result is None or result.intent.name not in ("HassMediaUnpause", "HassMediaPause")


def player(active=True):
    api = SimpleNamespace(command=AsyncMock(), transfer=AsyncMock(), search=AsyncMock(), play_uri=AsyncMock(),
        playback=AsyncMock(return_value={"device":{"id":"speaker"},"is_playing":True}))
    coordinator = SimpleNamespace(api=api, async_request_refresh=AsyncMock(), last_update_success=True,
        data=AccountState(devices={"studio": {"id": "speaker", "_spotify_id": "speaker", "name": "Studio"}},
            playback={"device": {"id": "speaker" if active else "elsewhere"}, "is_playing": False}))
    coordinator._playback_generation = 0
    def begin():
        coordinator._playback_generation += 1
        return coordinator._playback_generation
    coordinator.begin_playback_command = begin
    coordinator.is_current_playback_command = lambda generation: generation == coordinator._playback_generation
    coordinator.note_playback = lambda playback: setattr(coordinator.data, "playback", playback)
    return SpotifyConnectPlayer(coordinator, "studio"), api


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("phrase", ["music", "play music", "some music", "continue"])
def test_plain_play_media_resumes_or_transfers_without_catalogue_search(active, phrase):
    async def run():
        entity, api = player(active)
        await entity.async_play_media(MediaType.MUSIC, phrase)
        api.search.assert_not_called()
        api.play_uri.assert_not_called()
        if active:
            api.command.assert_awaited_once_with("play", "speaker")
            api.transfer.assert_not_called()
        else:
            api.transfer.assert_awaited_once_with("speaker", play=True)
            api.command.assert_not_called()
    asyncio.run(run())


def test_local_adapter_leaves_native_handlers_and_other_accounts_intact(tmp_path):
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import intent
    from homeassistant.components.media_player.intent import MediaSearchAndPlayHandler
    from custom_components.echomuse_spotify.intents import async_register_local_search, LOCAL_SEARCH

    async def run():
        hass = HomeAssistant(str(tmp_path))
        native = MediaSearchAndPlayHandler()
        intent.async_register(hass, native)
        cleanups = [[], []]
        for number in range(2):
            entry = SimpleNamespace(entry_id=str(number), async_on_unload=cleanups[number].append)
            async_register_local_search(hass, entry)
        handlers = {handler.intent_type: handler for handler in intent.async_get(hass)}
        assert handlers["HassMediaSearchAndPlay"] is native
        adapter = handlers[LOCAL_SEARCH]
        assert adapter.original is native
        cleanups[0][0]()
        assert adapter in intent.async_get(hass)
        cleanups[1][0]()
        assert not adapter.entries
        handlers = {handler.intent_type: handler for handler in intent.async_get(hass)}
        assert handlers["HassMediaSearchAndPlay"] is native
        assert handlers["EchoMuseSpotifySearchAndPlay"] is adapter
        assert not handlers["EchoMuseSpotifyShuffle"].entries
        assert not handlers["EchoMuseSpotifyRepeat"].entries
        # A second setup reuses the adapter without replacing any native
        # handler or retaining either unloaded account.
        entry = SimpleNamespace(entry_id="replacement", async_on_unload=lambda callback: None)
        async_register_local_search(hass, entry)
        assert adapter.entries == {"replacement"}
        assert adapter.original is native
        await hass.async_stop(force=True)
    asyncio.run(run())


def test_explicit_artist_named_music_remains_searchable():
    async def run():
        entity, api = player()
        uri = "spotify:artist:" + "m" * 22
        api.search.return_value = {"artists": {"items": [{"name": "Music", "uri": uri}]}}
        await entity.async_play_media(MediaType.ARTIST, "Music")
        assert api.search.call_args.args[0]["type"] == "artist"
        api.play_uri.assert_awaited_once_with("speaker", uri)
        api.command.assert_not_called()
    asyncio.run(run())


def test_explicit_uri_named_music_is_never_changed_into_resume():
    async def run():
        entity, api = player()
        uri = "spotify:track:" + "m" * 22
        await entity.async_play_media(MediaType.MUSIC, uri)
        api.play_uri.assert_awaited_once_with("speaker", uri)
        api.search.assert_not_called()
        api.command.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("uri", [
    "spotify:playlist:37i9dQZF1E-HkgMh927C6C",   # the invented id from a real tool call
    "spotify:track:short", "spotify:album:" + "a" * 23, "spotify:artist:" + "a" * 21,
])
def test_an_id_that_cannot_exist_is_refused_before_spotify_sees_it(uri):
    async def run():
        entity, api = player()
        with pytest.raises(HomeAssistantError, match="not a real Spotify ID"):
            await entity.async_play_media(MediaType.MUSIC, uri)
        api.play_uri.assert_not_called()
        api.search.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("uri", ["spotify:collection:tracks", "spotify:track:0KAiuUOrLTIkzkpfpn9jb9"])
def test_real_shaped_ids_and_other_uri_forms_still_play(uri):
    async def run():
        entity, api = player()
        await entity.async_play_media(MediaType.MUSIC, uri)
        api.play_uri.assert_awaited_once_with("speaker", uri)
    asyncio.run(run())


TRACK = "spotify:track:4kcMDgaU8xb4otoDv5c4kD"


def _playing(entity):
    entity.coordinator.data.playback = {"device": {"id": "speaker"}, "is_playing": True}


def test_a_models_repeat_of_what_it_just_started_does_not_restart_the_song():
    # Seen live: play_media("Drive"), then search_media, then play_media of the
    # top result 23s later. The model cannot see what the first call chose.
    async def run():
        entity, api = player()
        api.search = AsyncMock()
        entity.async_search_media = AsyncMock(return_value=SimpleNamespace(
            result=[SimpleNamespace(media_content_id=TRACK)]))
        await entity.async_play_media(MediaType.MUSIC, "Drive")
        _playing(entity)
        await entity.async_play_media(MediaType.MUSIC, TRACK)
        api.play_uri.assert_awaited_once_with("speaker", TRACK)
    asyncio.run(run())


def test_a_repeat_a_moment_later_is_ignored_even_before_the_poll_shows_it_playing():
    async def run():
        entity, api = player()
        await entity.async_play_media(MediaType.MUSIC, TRACK)
        await entity.async_play_media(MediaType.MUSIC, TRACK)       # state still reads paused
        api.play_uri.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("case", ["different track", "paused since", "long ago"])
def test_a_real_request_to_play_again_still_plays(case, monkeypatch):
    import custom_components.echomuse_spotify.media_player as mp

    async def run():
        entity, api = player()
        await entity.async_play_media(MediaType.MUSIC, TRACK)
        _playing(entity)
        uri = TRACK
        if case == "different track":
            uri = "spotify:track:0KAiuUOrLTIkzkpfpn9jb9"
        elif case == "paused since":
            entity.coordinator.data.playback = {"device": {"id": "speaker"}, "is_playing": False}
            entity._last_started = (TRACK, entity._last_started[1] - mp.JUST_STARTED_S - 1)
        else:
            entity._last_started = (TRACK, entity._last_started[1] - mp.REPEAT_PLAY_WINDOW_S - 1)
        await entity.async_play_media(MediaType.MUSIC, uri)
        assert api.play_uri.await_count == 2
    asyncio.run(run())


def _query(text, classes=None):
    from homeassistant.components.media_player import SearchMediaQuery
    return SearchMediaQuery(search_query=text, media_filter_classes=classes)


@pytest.mark.parametrize("text", ["music", "play music", "some music", "resume", "Music."])
def test_searching_a_generic_word_offers_resume_not_the_artist_named_music(text):
    # Seen live: search_media("music") then play_media of the top row, the
    # artist called Music.
    from custom_components.echomuse_spotify.playback import RESUME_URI

    async def run():
        entity, api = player()
        found = await entity.async_search_media(_query(text))
        api.search.assert_not_called()
        assert [r.media_content_id for r in found.result] == [RESUME_URI]
        assert found.result[0].can_play
        await entity.async_play_media(MediaType.MUSIC, found.result[0].media_content_id)
        api.play_uri.assert_not_called()
        api.command.assert_awaited_once_with("play", "speaker")
    asyncio.run(run())


def test_resume_offered_by_search_transfers_when_the_account_is_elsewhere():
    from custom_components.echomuse_spotify.playback import RESUME_URI

    async def run():
        entity, api = player(active=False)
        await entity.async_play_media(MediaType.MUSIC, RESUME_URI)
        api.transfer.assert_awaited_once_with("speaker", play=True)
        api.play_uri.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("text,classes", [
    ("Madonna", None),
    ("music", ["artist"]),      # "play the artist Music" stays literal
])
def test_real_searches_are_not_turned_into_resume(text, classes):
    from homeassistant.components.media_player import MediaClass

    async def run():
        entity, api = player()
        api.search.return_value = {}
        await entity.async_search_media(_query(text, [MediaClass.ARTIST] if classes else None))
        api.search.assert_awaited_once()
    asyncio.run(run())
