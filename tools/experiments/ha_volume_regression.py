"""Real HA volume parsing, targeting and services; simulated physical speakers.

Run in the Home Assistant test environment, with no physical device or Spotify
account required. The companion setup lifecycle is covered by test_schedule.py.
"""
import asyncio
from copy import deepcopy
from pathlib import Path
import shutil
from types import SimpleNamespace

from homeassistant.components import conversation
from homeassistant.components.conversation.models import ConversationInput
from homeassistant.components.conversation.chat_log import async_get_chat_log
from homeassistant.components.assist_pipeline.default_pipeline import _async_local_fallback_intent_filter
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.components.media_player import MediaPlayerEntityFeature, MediaPlayerState
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, intent
from homeassistant.helpers.chat_session import async_get_chat_session

from ha_command_regression import Speaker, add_entry
from ha_schedule_conversation import main


async def scenarios(hass):
    from custom_components.echomuse_schedule.volume import async_register_volume
    areas, devices, registry = ar.async_get(hass), dr.async_get(hass), er.async_get(hass)
    kitchen, bedroom = areas.async_create("Kitchen"), areas.async_create("Bedroom")
    source_entry = add_entry(hass, "esphome", "Volume sources", {})
    sources, satellites = [], []
    for name in ("A", "B", "Other"):
        device = devices.async_get_or_create(config_entry_id=source_entry.entry_id,
            identifiers={("esphome", "volume-"+name)}, name="Source "+name)
        devices.async_update_device(device.id, area_id=kitchen.id)
        sources.append(device)
        satellites.append(registry.async_get_or_create("assist_satellite", "esphome", "volume-"+name,
            config_entry=source_entry, device_id=device.id))
    players = []
    platforms = {}
    component = hass.data["media_player"]
    for name, platform, device_id, area in (
        ("Satellite A", "esphome", sources[0].id, kitchen),
        ("Satellite B", "esphome", sources[1].id, kitchen),
        ("Kitchen TV", "test", None, kitchen),
        ("Spotify speaker", "echomuse_spotify", None, kitchen),
        ("Bedroom TV", "test", None, bedroom),
    ):
        entity = registry.async_get_or_create("media_player", platform, "volume-"+name,
            suggested_object_id=name, config_entry=source_entry, device_id=device_id)
        registry.async_update_entity(entity.entity_id, area_id=area.id, name=name)
        model = Speaker()
        model._attr_unique_id = "volume-"+name
        model._attr_name = name
        if device_id:
            model._attr_device_info = {"identifiers": devices.async_get(device_id).identifiers}
        model.entity_id = entity.entity_id
        if platform not in platforms:
            platforms[platform] = component._async_init_entity_platform(platform, None)
            platforms[platform].config_entry = source_entry
            component._platforms[platform] = platforms[platform]
        await platforms[platform].async_add_entities([model])
        assert model.entity_id == entity.entity_id
        async_expose_entity(hass, "conversation", entity.entity_id, True)
        players.append(model)
    await hass.async_block_till_done()
    originals = {h.intent_type:h for h in intent.async_get(hass)}
    coordinators = {str(n):SimpleNamespace(device_id=sources[n].id) for n in (0, 1)}

    def reset(playing=None):
        for player in players:
            player.reset()
            player._attr_state = MediaPlayerState.PLAYING if player is playing else MediaPlayerState.IDLE
            player.async_write_ha_state()

    async def say(text, local=False, source=0):
        if local:
            user = ConversationInput(text=text, context=Context(), conversation_id=None,
                language="en", device_id=sources[source].id, satellite_id=satellites[source].entity_id,
                agent_id="conversation.volume_test_llm")
            with async_get_chat_session(hass, None) as session, async_get_chat_log(hass, session, user) as log:
                response = await conversation.async_handle_intents(hass, user, log,
                    intent_filter=_async_local_fallback_intent_filter)
        else:
            response = (await conversation.async_converse(hass, text, None, Context(), language="en",
                device_id=sources[source].id, satellite_id=satellites[source].entity_id)).response
        await hass.async_block_till_done()
        return response

    def events():
        return tuple(tuple(p.events) for p in players)

    explicit = ["set Kitchen TV volume to 50 percent", "set Kitchen volume to 50 percent",
        "set the volume to 50 percent in Bedroom", "volume up in Kitchen",
        "increase Bedroom volume by 10 percent",
        # HA's full conversation matcher can tolerate unmatched prefix words;
        # compare its actual behavior rather than assuming strict recognition.
        "don't set volume to 50 percent", "do not turn up the volume"]
    implicit = ["set volume to 50 percent", "set the volume to 25 percent", "volume up",
        "turn down the volume", "increase the volume by 10 percent"]
    baseline = {}
    for local in (False, True):
        for playing in (None, players[2]):
            for text in explicit+implicit:
                reset(playing)
                response = await say(text, local)
                baseline[local, bool(playing), text] = (response is not None and response.error_code is None, events())
    reset()
    response = await say("volume 50%", True)
    assert response is None or response.error_code is not None, (response.as_dict(), events())
    assert not any(events())
    print("PASS HA baseline: shorthand falls through; ordinary absolute volume works while idle", flush=True)
    target = Path(hass.config.config_dir)/"custom_sentences/en/echomuse_schedule.yaml"
    shutil.copy(Path(__file__).resolve().parents[2]/"custom_sentences/en/echomuse_schedule.yaml", target)
    async_register_volume(hass, coordinators, originals)
    await conversation.async_get_agent(hass).async_reload()
    assert all(next(h for h in intent.async_get(hass) if h.intent_type == name) is original for name, original in originals.items() if name not in ("HassSetVolume", "HassSetVolumeRelative"))
    checked = 0
    for local in (False, True):
        for playing in (None, players[2]):
            for text in explicit + (implicit if playing else []):
                reset(playing)
                response = await say(text, local)
                assert (response is not None and response.error_code is None, events()) == baseline[local, bool(playing), text], (local, playing, text,
                    response.as_dict() if response else None, events(), baseline[local,bool(playing),text])
                checked += 1
        for origin in (0, 1):
            for text, expected in [("volume 50%", 0.5), ("volume fifty percent", 0.5),
                    ("volume 0 percent", 0), ("volume 100 percent", 1),
                    ("set volume to 50 percent", 0.5), ("set the volume to 25 percent", 0.25),
                    ("volume up", 0.6), ("turn down the volume", 0.4),
                    ("increase the volume by 10 percent", 0.6)]:
                for playing in (None, players[4]):  # Playback in another room must not redirect volume.
                    reset(playing)
                    response = await say(text, local, origin)
                    assert response is not None and response.error_code is None, (text, local, response.as_dict() if response else None,
                        [(e.entity_id,e.platform,e.device_id) for e in er.async_entries_for_device(registry,sources[origin].id)])
                    assert events()[origin] == (("volume", expected),), (text, origin, events())
                    assert not any(p.events for n,p in enumerate(players) if n != origin), (text, origin, events())
                    checked += 1
        for text in ("volume 101 percent", "volume -1 percent", "play volume 50", "turn on Kitchen TV"):
            reset()
            await say(text, local)
            assert not any(event[0] == "volume" for p in players for event in p.events), (text, events())
    print(f"PASS {checked} volume cases in native conversation and local-before-LLM modes; explicit targets and active-room behavior unchanged", flush=True)
    original_features = players[0].supported_features
    for mode in ("hidden", "unavailable", "unsupported", "ambiguous"):
        reset()
        if mode == "hidden": async_expose_entity(hass, "conversation", players[0].entity_id, False)
        if mode == "unavailable": hass.states.async_set(players[0].entity_id, "unavailable")
        if mode == "unsupported":
            # Features are a cached property on HA entities. Change the fake
            # entity as well as its registry entry; a state-only edit is undone
            # by HA's entity-registry update listener.
            players[0].__dict__["supported_features"] = MediaPlayerEntityFeature(0)
            registry.async_update_entity(players[0].entity_id, supported_features=0)
            players[0].async_write_ha_state()
        if mode == "ambiguous":
            extra = registry.async_get_or_create("media_player", "esphome", "ambiguous-volume",
                device_id=sources[0].id, config_entry=source_entry)
            hass.states.async_set(extra.entity_id, "idle", {"supported_features":int(MediaPlayerEntityFeature.VOLUME_SET)})
            async_expose_entity(hass, "conversation", extra.entity_id, True)
        assert await say("volume 50%", True) is None, mode
        assert not any(events()), (mode, events())
        async_expose_entity(hass, "conversation", players[0].entity_id, True)
        players[0].__dict__["supported_features"] = original_features
        registry.async_update_entity(players[0].entity_id, supported_features=int(original_features))
        if mode == "ambiguous":
            hass.states.async_remove(extra.entity_id)
            registry.async_remove(extra.entity_id)
    print("PASS hidden, offline, unsupported and duplicate source players never adjust another device", flush=True)
    reset()
    async def fail(volume):
        players[0].events.append(("failed_volume",volume))
        raise HomeAssistantError("simulated device failure")
    original_set = players[0].async_set_volume_level
    players[0].async_set_volume_level = fail
    response = await say("volume 50%", True)
    assert response is None or response.error_code is not None, (response.as_dict(), events())
    assert players[0].events == [("failed_volume",0.5)] and not any(p.events for p in players[1:])
    players[0].async_set_volume_level = original_set
    print("PASS failed device call is attempted once without redirecting to other players", flush=True)
    # Unbound satellites and dormant handlers preserve HA behavior. Reload rebinds
    # the fresh coordinator dictionary without replacing any native handlers.
    for local in (False, True):
        reset()
        response = await say("set volume to 50 percent", local, 2)
        assert (response is not None and response.error_code is None, events()) == baseline[local,False,"set volume to 50 percent"]
    coordinators.clear()
    reset()
    response = await say("set volume to 50 percent", True)
    assert response is not None and response.error_code is None
    assert events() == baseline[True,False,"set volume to 50 percent"][1]
    async_register_volume(hass, {"A":SimpleNamespace(device_id=sources[0].id)}, originals)
    reset()
    assert (await say("volume 50%", True)).error_code is None
    assert events()[0] == (("volume",0.5),) and not any(p.events for p in players[1:])
    print("PASS unbound satellites, dormant adapters and reload; unrelated native handlers retained", flush=True)


if __name__ == "__main__":
    asyncio.run(main(scenario=scenarios, install_sentences=False, scenario_timeout=180))
