"""Entity identity is derived from the device NAME, never from Spotify's id."""
import asyncio
from types import SimpleNamespace

import pytest

from custom_components.echomuse_spotify.coordinator import (
    SpotifyCoordinator,
    entity_slug,
)


def bare_coordinator():
    c = object.__new__(SpotifyCoordinator)
    c._key_for_id, c._id_for_key, c._name_for_key, c._seen_by_key = {}, {}, {}, {}
    return c


@pytest.mark.parametrize("name,expected", [
    ("Living Room", "living_room"),
    ("  Living   ROOM ", "living_room"),
    ("Küche", "küche"),
    ("キッチン", "キッチン"),
    ("Echo-Dot (2)", "echo_dot_2"),
])
def test_slug_keeps_letters_of_any_script(name, expected):
    assert entity_slug(name) == expected


def test_a_name_with_no_letters_hashes_the_name_not_the_spotify_id():
    a, b = entity_slug("🔊"), entity_slug(" 🔊 ")
    assert a == b and a.startswith("device_") and len(a) == len("device_") + 8
    assert a != entity_slug("🎵")
    assert entity_slug("") == ""


@pytest.mark.parametrize("name", ["Living Room", "キッチン", "🔊"])
def test_entity_key_survives_a_spotify_id_change(name):
    c = bare_coordinator()
    first = c._bind_devices({"id-one": {"id": "id-one", "name": name}})
    (key,) = first
    # The session dropped; Spotify offers the same speaker under a new id.
    second = c._bind_devices({"id-two": {"id": "id-two", "name": name}})
    assert list(second) == [key]
    assert second[key]["_spotify_id"] == "id-two"
    assert not second[key].get("_absent")


def test_missing_native_search_intent_retries_instead_of_crashing(tmp_path):
    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import ConfigEntryNotReady
    from custom_components.echomuse_spotify.intents import async_register_local_search

    async def run():
        hass = HomeAssistant(str(tmp_path))
        entry = SimpleNamespace(entry_id="1", async_on_unload=lambda callback: None)
        with pytest.raises(ConfigEntryNotReady):
            async_register_local_search(hass, entry)
        await hass.async_stop(force=True)
    asyncio.run(run())
