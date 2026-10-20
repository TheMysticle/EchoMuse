"""Editing a reminder from the Home Assistant to-do card.

The card sends the whole item back, so most of this is deciding what did NOT
change: the device resets a reminder to scheduled when its deadline moves, so a
rename must leave the deadline alone, and a time the card rounded to the minute
is not a move.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.todo import TodoItem, TodoItemStatus, TodoListEntityFeature
from homeassistant.exceptions import HomeAssistantError

from custom_components.echomuse_schedule.todo import Reminders, changed_reminder

COPY = "sh-0123abcd-0123456789abcdef01234567"
DUE = 1999999999 - 1999999999 % 60 + 3600 * 24 * 400     # a whole minute, far ahead


def reminder(**over):
    item = {"id": "r1", "kind": "reminder", "label": "take out the bins", "message": "take out the bins",
            "state": "scheduled", "due": DUE + 17, "timezone": "Europe/Warsaw", "speech": "a", "speech_version": 1}
    item.update(over)
    return item


def card(**over):
    fields = {"uid": "r1", "summary": "take out the bins", "status": TodoItemStatus.NEEDS_ACTION,
              "due": datetime.fromtimestamp(DUE, timezone.utc)}
    fields.update(over)
    return TodoItem(**fields)


def test_the_card_offers_a_time_picker_with_the_time():
    features = Reminders.__new__(Reminders).supported_features
    assert features & TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
    assert features & TodoListEntityFeature.UPDATE_TODO_ITEM


def test_the_card_can_send_the_description_back():
    # Without SET_DESCRIPTION_ON_ITEM Home Assistant refuses any update that
    # carries a description ("Entity does not support setting field"), and the
    # card always sends the one it was shown.
    assert Reminders.__new__(Reminders).supported_features & TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    new = changed_reminder(reminder(), card(summary="x", description="scheduled"))
    assert new["message"] == "x"
    assert changed_reminder(reminder(rule={"days": [1], "hour": 7, "minute": 0}, due=DUE),
                            card(description="scheduled; recurring")) is None


def test_a_description_that_was_changed_is_refused_not_dropped():
    with pytest.raises(HomeAssistantError, match="cannot be edited"):
        changed_reminder(reminder(), card(summary="x", description="my notes"))
    with pytest.raises(HomeAssistantError, match="cannot be edited"):
        changed_reminder(reminder(), card(description=""))


def test_nothing_changed_sends_nothing():
    # The card rounds 12:00:17 to 12:00; that is the same reminder.
    assert changed_reminder(reminder(), card()) is None


def test_renaming_leaves_the_deadline_alone_and_drops_the_cached_speech():
    new = changed_reminder(reminder(), card(summary="feed the cat"))
    assert new["message"] == "feed the cat" and new["label"] == "feed the cat"
    assert new["due"] == DUE + 17
    assert "speech" not in new and "speech_version" not in new


def test_a_label_the_user_chose_survives_a_rename():
    assert changed_reminder(reminder(label="Mum"), card(summary="x"))["label"] == "Mum"


def test_moving_sets_the_deadline_and_keeps_the_words_and_speech():
    new = changed_reminder(reminder(), card(due=datetime.fromtimestamp(DUE + 7200, timezone.utc)))
    assert new["due"] == DUE + 7200 and new["speech"] == "a"


@pytest.fixture
def warsaw():
    from zoneinfo import ZoneInfo
    from homeassistant.util import dt as dt_util
    old = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo("Europe/Warsaw"))
    yield
    dt_util.set_default_time_zone(old)


def test_a_naive_time_is_the_wall_clock_of_home_assistant_not_the_reminders_zone(warsaw):
    # 22:00 on the card, in January (UTC+1). A reminder saved in "UTC" (a model
    # passes that) used to read it as 22:00 UTC, which is midnight.
    wall = datetime(2030, 1, 15, 22, 0)
    for zone in ("Europe/Warsaw", "UTC", "America/New_York"):
        new = changed_reminder(reminder(timezone=zone), card(due=wall))
        assert datetime.fromtimestamp(new["due"], timezone.utc) == datetime(2030, 1, 15, 21, 0, tzinfo=timezone.utc), zone


def test_seconds_the_picker_does_not_have_are_dropped(warsaw):
    new = changed_reminder(reminder(), card(due=datetime(2030, 1, 15, 22, 2, 51)))
    assert new["due"] % 60 == 0


def test_a_reminder_saved_in_utc_is_moved_into_the_zone_it_is_set_in(warsaw):
    assert changed_reminder(reminder(timezone="UTC"), card(due=datetime(2030, 1, 15, 22, 0)))["timezone"] == "Europe/Warsaw"
    assert changed_reminder(reminder(timezone="UTC"), card(summary="x"))["timezone"] == "UTC"     # a rename is not a move
    assert changed_reminder(reminder(timezone="America/New_York"), card(due=datetime(2030, 1, 15, 22, 0)))["timezone"] == "America/New_York"


def test_the_original_is_not_modified():
    current = reminder()
    changed_reminder(current, card(summary="x", due=datetime.fromtimestamp(DUE + 7200, timezone.utc)))
    assert current == reminder()


@pytest.mark.parametrize("current,fields,match", [
    ({"state": "completed"}, {"summary": "x"}, "finished"),
    ({"state": "missed"}, {"due": datetime.fromtimestamp(DUE + 7200, timezone.utc)}, "finished"),
    ({"id": COPY}, {"summary": "x"}, "shared"),
    ({"rule": {"days": [1], "hour": 7, "minute": 0}}, {"due": datetime.fromtimestamp(DUE + 7200, timezone.utc)}, "repeating"),
    ({}, {"due": None}, "needs a time"),
])
def test_edits_that_would_be_wrong_are_refused(current, fields, match):
    with pytest.raises(HomeAssistantError, match="(?i)" + match):
        changed_reminder(reminder(**current), card(**fields))


def test_a_repeating_reminder_can_still_be_renamed():
    rule = {"days": [1], "hour": 7, "minute": 0}
    new = changed_reminder(reminder(rule=rule, due=DUE), card(summary="x"))
    assert new["message"] == "x" and new["rule"] == rule


def entity(items):
    coordinator = SimpleNamespace(data={"items": items}, command=AsyncMock(return_value={}))
    entity = Reminders.__new__(Reminders)
    entity.coordinator = coordinator
    return entity, coordinator


def test_update_sends_an_edit_for_a_change_and_nothing_for_none():
    async def run():
        e, c = entity([reminder()])
        await e.async_update_todo_item(card())
        c.command.assert_not_awaited()
        await e.async_update_todo_item(card(summary="feed the cat"))
        sent = c.command.await_args.args[0]
        assert sent["action"] == "edit" and sent["id"] == "r1" and sent["item"]["message"] == "feed the cat"
    asyncio.run(run())


def test_completing_still_completes_and_ignores_other_fields():
    async def run():
        e, c = entity([reminder()])
        await e.async_update_todo_item(card(status=TodoItemStatus.COMPLETED, summary="renamed"))
        c.command.assert_awaited_once_with({"action": "complete", "id": "r1"})
        e, c = entity([reminder(state="completed")])
        await e.async_update_todo_item(card(status=TodoItemStatus.COMPLETED))
        c.command.assert_not_awaited()                # already finished; nothing to do
    asyncio.run(run())


def test_reopening_a_finished_reminder_is_refused_not_ignored():
    async def run():
        e, c = entity([reminder(state="completed")])
        with pytest.raises(HomeAssistantError, match="finished"):
            await e.async_update_todo_item(card(summary="x"))
        c.command.assert_not_awaited()
        with pytest.raises(HomeAssistantError, match="not found"):
            await e.async_update_todo_item(card(uid="nope"))
    asyncio.run(run())


def test_home_assistants_own_field_check_accepts_what_the_card_sends():
    # _validate_supported_features raises "Entity does not support setting field:
    # description" for any field whose required feature the entity lacks.
    from homeassistant.components.todo.services import TODO_ITEM_FIELDS
    features = Reminders.__new__(Reminders).supported_features
    sent = {"description", "due_datetime"}
    needed = {d.service_field: d.required_feature for d in TODO_ITEM_FIELDS}
    assert sent <= set(needed)
    assert all(features & needed[field] for field in sent)
