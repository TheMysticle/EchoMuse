"""Run in an isolated HA 2026.10.1 environment (see documentation)."""
import asyncio
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo
import pytest
from homeassistant.core import HomeAssistant, Context
from homeassistant.helpers import intent
from homeassistant.components.intent.timers import TimerManager
from homeassistant.components.intent.const import TIMER_DATA
from hassil import Intents, recognize_best
from hassil.util import merge_dict
from home_assistant_intents import get_intents
import yaml
from custom_components.echomuse_schedule.language import parse_item, duration
from custom_components.echomuse_schedule.intents import Handler, TIMERS, source

NOW = datetime(2026, 10, 15, 8, tzinfo=ZoneInfo("Europe/Warsaw"))
ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("text,seconds", [("five minutes", 300), ("an hour and a half", 5400),
    ("half an hour", 1800), ("two hours and thirty minutes", 9000),
    ("90 seconds", 90), ("twenty-five minutes", 1500), ("one day", 86400)])
def test_durations(text, seconds):
    assert duration(text) == (seconds, "")


@pytest.mark.parametrize("text,kind,expected", [
    ("to take medicine in five minutes", "reminder", {"message": "take medicine", "duration": 300}),
    ("at seven tomorrow morning", "alarm", {"due": 1792126800}),
    ("every weekday at 7am", "alarm", {"rule": {"days": [1, 2, 3, 4, 5], "hour": 7, "minute": 0}}),
    ("to call Sam every two hours", "reminder", {"message": "call Sam", "rule": {"interval": 7200, "hour": 0, "minute": 0}}),
    ("tomorrow at 9am to take bins out", "reminder", {"message": "take bins out"}),
])
def test_parse(text, kind, expected):
    item = parse_item(kind, text, "Europe/Warsaw", NOW)
    for key, value in expected.items():
        assert item[key] == value


@pytest.mark.parametrize("text", ["at seven", "at 25:00", "sometime next month", "at 7:99pm"])
def test_ambiguous_or_invalid_time(text):
    with pytest.raises(ValueError):
        parse_item("alarm", text, "Europe/Warsaw", NOW)


def grammar():
    builtins = deepcopy(get_intents("en"))
    custom = yaml.safe_load((ROOT / "custom_sentences/en/echomuse_schedule.yaml").read_text())
    for definition in custom["intents"].values():
        for data in definition["data"]:
            data["metadata"] = {"custom_sentence": True}
    merge_dict(builtins, custom)
    for key in ("name", "area", "floor", "domain", "device_class"):
        builtins["lists"].setdefault(key, {"values": []})
    return Intents.from_dict(builtins)


@pytest.mark.parametrize("text,expected", [
    ("set a timer for five minutes", "EchoMuseCreate"),
    ("set a pasta timer for 12 minutes", "EchoMuseCreate"),
    ("remind me to call Sam in two hours", "EchoMuseCreate"),
    ("in five minutes remind me to take medicine", "EchoMuseCreate"),
    ("wake me up at seven tomorrow morning", "EchoMuseCreate"),
    ("set an alarm for every weekday at 7am", "EchoMuseCreate"),
    ("cancel all my timers", "EchoMuseAction"),
    ("list my reminders", "EchoMuseList"),
    ("snooze for ten minutes", "EchoMuseAction"),
    ("stop ringing", "EchoMuseAction"),
])
def test_real_hassil_custom_priority(text, expected):
    result = recognize_best(text, grammar(), language="en", best_metadata_key="custom_sentence")
    assert result is not None and result.intent.name == expected


def test_does_not_capture_unrelated_commands():
    for text in ("turn on the kitchen light", "set the thermostat to twenty", "play jazz"):
        result = recognize_best(text, grammar(), language="en", best_metadata_key="custom_sentence")
        assert result is None or not result.intent.name.startswith("EchoMuse")


class FakeCoordinator:
    device_id = "device-a"
    last_update_success = True
    def __init__(self):
        self.data = {"items": [{"id": "one", "kind": "timer", "state": "scheduled", "label": "tea", "due": 1999999999, "duration": 60}], "active": ""}
        self.commands = []
    async def command(self, command):
        self.commands.append(command)
        if command["action"] == "create":
            self.data["items"].append({**command["item"], "id": "new", "due": 1999999999, "timezone": "UTC"})
        return {"snapshot": self.data}


def test_real_ha_dispatch_replaces_all_timer_handlers_without_ha_countdowns(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.config.time_zone = "Europe/Warsaw"
        manager = TimerManager(hass)
        hass.data[TIMER_DATA] = manager
        coordinator = FakeCoordinator()
        for name, action in TIMERS.items():
            from homeassistant.components.intent import timers
            original = next(cls() for cls in (timers.StartTimerIntentHandler, timers.CancelTimerIntentHandler,
                timers.CancelAllTimersIntentHandler, timers.IncreaseTimerIntentHandler, timers.DecreaseTimerIntentHandler,
                timers.PauseTimerIntentHandler, timers.UnpauseTimerIntentHandler, timers.TimerStatusIntentHandler)
                if cls.intent_type == name)
            handler = Handler(name, {"a": coordinator}, original)
            intent.async_register(hass, handler)
            slots = {"minutes": {"value": 1}}
            if name not in ("HassStartTimer", "HassIncreaseTimer", "HassDecreaseTimer"):
                slots = {}
            coordinator.data["items"] = coordinator.data["items"][:1]
            before = len(coordinator.commands)
            response = await intent.async_handle(hass, "conversation", name, slots, "test", Context(), "en", device_id="device-a")
            assert response.speech, name
            if action != "list":
                assert len(coordinator.commands) == before + 1, (name, response.speech)
                assert coordinator.commands[-1]["action"] == action
            assert manager.timers == {}, name
        await hass.async_stop(force=True)
    asyncio.run(run())


def test_source_identity_rejects_conflicting_device_ids():
    c = FakeCoordinator()
    obj = SimpleNamespace(device_id="device-a", satellite_id="assist_satellite.other")
    registry = SimpleNamespace(async_get=lambda entity: SimpleNamespace(device_id="device-b"))
    with patch("custom_components.echomuse_schedule.intents.er.async_get", return_value=registry):
        with pytest.raises(ValueError, match="Conflicting"):
            source(None, obj, {"a": c})


def test_live_entity_creation_and_registry_cleanup(tmp_path):
    async def run():
        from homeassistant.helpers import entity_registry as er, device_registry as dr, area_registry as ar, floor_registry as fr
        from homeassistant.config_entries import ConfigEntry, ConfigEntries
        from custom_components.echomuse_schedule import sensor
        hass = HomeAssistant(str(tmp_path))
        hass.config_entries = ConfigEntries(hass, {})
        hass.config_entries._store.set_load_empty()
        await hass.config_entries.async_initialize()
        hass.data[dr.DATA_REGISTRY] = dr.DeviceRegistry(hass)
        await fr.async_load(hass, load_empty=True)
        await ar.async_load(hass, load_empty=True)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
        entry = ConfigEntry(domain="echomuse_schedule", title="EchoMuse A", data={"satellite": "A"},
            version=1, minor_version=1, source="user", unique_id="A", options={}, subentries_data=[], discovery_keys={})
        hass.config_entries._entries[entry.entry_id] = entry
        listeners = []
        entry.runtime_data = coordinator = SimpleNamespace(entry=entry,
            data={"items": [{"id": "one", "kind": "timer", "state": "scheduled", "label": "tea"}]},
            last_update_success=True, async_add_listener=lambda cb: listeners.append(cb) or (lambda: None))
        added = []
        registry = er.async_get(hass)
        def add(entities):
            for entity in entities:
                added.append(entity)
                registry.async_get_or_create("sensor", "echomuse_schedule", entity.unique_id, config_entry=entry)
        await sensor.async_setup_entry(hass, entry, add)
        assert len(added) == 1
        assert registry.async_get_entity_id("sensor", "echomuse_schedule", "A_one")
        coordinator.data = {"items": [*coordinator.data["items"], {"id": "two", "kind": "alarm", "state": "scheduled", "label": "wake"}]}
        listeners[0]()
        assert len(added) == 2
        coordinator.last_update_success = False
        coordinator.data = {"items": []}
        listeners[0]()
        assert registry.async_get_entity_id("sensor", "echomuse_schedule", "A_one"), "offline must not remove items"
        coordinator.last_update_success = True
        listeners[0]()
        assert registry.async_get_entity_id("sensor", "echomuse_schedule", "A_one") is None
        assert registry.async_get_entity_id("sensor", "echomuse_schedule", "A_two") is None
        await hass.async_stop(force=True)
    asyncio.run(run())


def test_unrelated_satellite_delegates_to_original_handler(tmp_path):
    async def run():
        from unittest.mock import AsyncMock
        hass = HomeAssistant(str(tmp_path))
        original = SimpleNamespace(description="Original", slot_schema=None, async_handle=AsyncMock(return_value="original result"))
        handler = Handler("HassStartTimer", {"a": FakeCoordinator()}, original)
        obj = SimpleNamespace(hass=hass, device_id="unrelated", satellite_id=None, create_response=lambda: SimpleNamespace())
        assert await handler.async_handle(obj) == "original result"
        original.async_handle.assert_awaited_once_with(obj)
        await hass.async_stop(force=True)
    asyncio.run(run())

@pytest.mark.parametrize("text,expected", [
    ("what is my next alarm", "EchoMuseList"),
    ("give me five more minutes", "EchoMuseAction"),
    ("remind me again in half an hour", "EchoMuseAction"),
    ("cancel the reminder to call Sam", "EchoMuseAction"),
])
def test_more_natural_phrases(text, expected):
    result = recognize_best(text, grammar(), language="en", best_metadata_key="custom_sentence")
    assert result is not None and result.intent.name == expected


def test_setup_maps_esphome_source_and_unload_restores_handlers(tmp_path):
    async def run():
        from unittest.mock import AsyncMock
        from homeassistant.config_entries import ConfigEntry, ConfigEntries
        from homeassistant.helpers import device_registry as dr
        from homeassistant.components.intent import timers
        from custom_components import echomuse_schedule as integration
        hass = HomeAssistant(str(tmp_path))
        hass.config_entries = ConfigEntries(hass, {})
        hass.config_entries._store.set_load_empty()
        await hass.config_entries.async_initialize()
        registry = dr.DeviceRegistry(hass)
        hass.data[dr.DATA_REGISTRY] = registry
        await registry.async_load(load_empty=True)
        def entry(domain, serial, data):
            value = ConfigEntry(domain=domain, title=serial, data=data, version=1, minor_version=1,
                source="user", unique_id=serial, options={}, subentries_data=[], discovery_keys={})
            hass.config_entries._entries[value.entry_id] = value
            return value
        esphome = entry("esphome", "A", {})
        mac = "02:00:00:00:00:01"
        original_device = registry.async_get_or_create(config_entry_id=esphome.entry_id,
            connections={(dr.CONNECTION_NETWORK_MAC, mac)}, identifiers={("esphome", mac)}, name="Voice A")
        schedule_entry = entry("echomuse_schedule", "A schedules", {"satellite": "A", "mac": mac})
        originals = {}
        from homeassistant.components.media_player.intent import async_setup_intents
        await async_setup_intents(hass)
        originals.update({h.intent_type:h for h in intent.async_get(hass)})
        for cls in (timers.StartTimerIntentHandler, timers.CancelTimerIntentHandler,
                timers.CancelAllTimersIntentHandler, timers.IncreaseTimerIntentHandler, timers.DecreaseTimerIntentHandler,
                timers.PauseTimerIntentHandler, timers.UnpauseTimerIntentHandler, timers.TimerStatusIntentHandler):
            handler = cls()
            originals[handler.intent_type] = handler
            intent.async_register(hass, handler)
        class TestCoordinator:
            def __init__(self, hass, config, api):
                self.entry = config
                self.last_update_success = True
            async_config_entry_first_refresh = AsyncMock()
            def async_add_listener(self, listener):
                return lambda: None
        with patch.object(integration, "Coordinator", TestCoordinator), \
             patch.object(integration, "async_get_clientsession", return_value=None), \
             patch.object(hass.config_entries, "async_forward_entry_setups", new=AsyncMock()), \
             patch.object(hass.config_entries, "async_unload_platforms", new=AsyncMock(return_value=True)):
            assert await integration.async_setup_entry(hass, schedule_entry)
            assert schedule_entry.runtime_data.device_id == original_device.id
            own = [d for d in registry.devices if d.config_entry_id == schedule_entry.entry_id]
            assert len(own) == 1 and own[0].id != original_device.id
            assert own[0].via_device_id == original_device.id
            assert all(isinstance(h, Handler) for h in intent.async_get(hass) if h.intent_type in TIMERS)
            assert await integration.async_unload_entry(hass, schedule_entry)
            restored = {h.intent_type: h for h in intent.async_get(hass)}
            assert all(restored[k] is v for k, v in originals.items())
        await hass.async_stop(force=True)
    asyncio.run(run())


def test_speech_caching_does_not_delay_the_initial_device_commit(tmp_path):
    async def run():
        from unittest.mock import AsyncMock, Mock
        from custom_components.echomuse_schedule.coordinator import Coordinator
        hass = HomeAssistant(str(tmp_path))
        hass.config.time_zone = "Europe/Warsaw"
        coordinator = object.__new__(Coordinator)
        coordinator.hass = hass
        coordinator.data = {"items": []}
        coordinator.command_lock = asyncio.Lock()
        coordinator.last_created = {}
        coordinator.api = SimpleNamespace(command=AsyncMock(return_value={"items": []}), asset=AsyncMock())
        coordinator.async_set_updated_data = Mock()
        coordinator.entry = SimpleNamespace(data={})
        with patch("custom_components.echomuse_schedule.coordinator.generate_media_source_id") as generate:
            result = await coordinator.command({"action": "create", "item": {"kind": "reminder", "message": "take medicine", "duration": 300}})
            generate.assert_not_called()
            coordinator.api.command.assert_awaited_once()
            assert "not ready for offline" in result["warning"]
        await hass.async_stop(force=True)
    asyncio.run(run())

@pytest.mark.parametrize("text,seconds", [("one hundred and fifty minutes", 9000), ("two and a half hours", 9000), ("two hours and a half", 9000)])
def test_compound_number_durations(text, seconds):
    assert duration(text) == (seconds, "")

@pytest.mark.parametrize("text", ["-5 minutes", "minus five minutes"])
def test_negative_durations_are_not_silently_made_positive(text):
    with pytest.raises(ValueError):
        duration(text)

def test_unsupported_calendar_words_are_not_ignored():
    with pytest.raises(ValueError):
        parse_item("alarm", "at 7am next month", "Europe/Warsaw", NOW)
