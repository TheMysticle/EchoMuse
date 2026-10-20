"""Copies made by the controller's schedule sharing: listed as reminders, left out of the sensors."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.echomuse_schedule.shared import is_shared

COPY = "sh-0123abcd-0123456789abcdef01234567"


@pytest.mark.parametrize("item_id,expected", [
    (COPY, True),
    ("sh-0123abcd-0123456789abcdef0123456", False),     # 23 hex
    ("sh-0123abcd-0123456789abcdef012345678", False),    # 25 hex
    ("sh-0123ABCD-0123456789abcdef01234567", False),     # the controller mints lower case
    ("xsh-0123abcd-0123456789abcdef01234567", False),
    (COPY + "\n", False),
    ("", False),
    ("0123456789abcdef0123456789abcdef", False),         # an ordinary item id
])
def test_only_the_controllers_copy_id_is_shared(item_id, expected):
    assert is_shared({"id": item_id}) is expected


def test_missing_or_odd_ids_are_not_shared():
    assert is_shared({}) is False
    assert is_shared({"id": None}) is False
    assert is_shared({"id": 5}) is False


def test_reminder_list_shows_copies_marked_shared():
    from custom_components.echomuse_schedule.todo import Reminders
    entry = SimpleNamespace(data={"satellite": "A"})
    coordinator = SimpleNamespace(entry=entry, data={"items": [
        {"id": "own", "kind": "reminder", "state": "scheduled", "label": "own", "due": 1999999999},
        {"id": COPY, "kind": "reminder", "state": "scheduled", "label": "copy", "due": 1999999999,
         "message": "collect brother from the airport", "rule": {"hour": 7, "minute": 0}},
        {"id": "t", "kind": "timer", "state": "scheduled", "label": "tea"},
    ]})
    entity = Reminders.__new__(Reminders)
    entity.coordinator = coordinator
    items = entity.todo_items
    assert [i.uid for i in items] == ["own", COPY]
    assert items[0].description == "scheduled"
    assert items[1].summary == "collect brother from the airport"
    assert items[1].description == "scheduled; recurring; shared"


def test_sensors_skip_copies_but_keep_everything_else(tmp_path):
    async def run():
        from homeassistant.core import HomeAssistant
        from custom_components.echomuse_schedule import sensor
        hass = HomeAssistant(str(tmp_path))
        entry = SimpleNamespace(data={"satellite": "A"}, entry_id="e", async_on_unload=lambda cb: None)
        coordinator = SimpleNamespace(entry=entry,
            data={"items": [
                {"id": "own", "kind": "timer", "state": "scheduled", "label": "tea"},
                {"id": COPY, "kind": "timer", "state": "scheduled", "label": "tea"},
                {"id": "sh-0123abcd-" + "f" * 24, "kind": "alarm", "state": "scheduled", "label": "wake"},
            ]},
            last_update_success=True, async_add_listener=lambda cb: (lambda: None))
        entry.runtime_data = coordinator
        added = []
        registry = MagicMock()
        with patch.object(sensor.er, "async_get", return_value=registry), \
                patch.object(sensor.er, "async_entries_for_config_entry", return_value=[]):
            await sensor.async_setup_entry(hass, entry, lambda entities: added.extend(entities))
        assert [e.unique_id for e in added] == ["A_own"]
        await hass.async_stop(force=True)
    asyncio.run(run())


def test_speech_retry_ignores_copies_but_still_retries_own_reminders(tmp_path):
    async def run():
        from homeassistant.core import HomeAssistant
        from custom_components.echomuse_schedule.coordinator import Coordinator
        hass = HomeAssistant(str(tmp_path))
        base = {"kind": "reminder", "state": "scheduled", "message": "call Sam"}
        snapshot = {"revision": 1, "items": [{**base, "id": COPY}]}
        api = SimpleNamespace(snapshot=AsyncMock(return_value={"snapshot": snapshot}))
        entry = MagicMock()
        coordinator = Coordinator.__new__(Coordinator)
        coordinator.entry, coordinator.api, coordinator.hass = entry, api, hass
        coordinator.speech_retry_after, coordinator.speech_task = {}, None

        def spawn(_hass, coro, _name):
            coro.close()
            return SimpleNamespace(done=lambda: False)
        entry.async_create_background_task.side_effect = spawn

        await coordinator._async_update_data()
        entry.async_create_background_task.assert_not_called()
        assert coordinator.speech_retry_after == {}

        snapshot["items"].append({**base, "id": "own"})
        await coordinator._async_update_data()
        entry.async_create_background_task.assert_called_once()
        assert list(coordinator.speech_retry_after) == ["own"]
        await hass.async_stop(force=True)
    asyncio.run(run())
