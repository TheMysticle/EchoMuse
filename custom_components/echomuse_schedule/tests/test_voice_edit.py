"""Moving a reminder or alarm by voice through Home Assistant's own Assist.

Real grammar (custom_sentences/en/echomuse_schedule.yaml), real handler, a
coordinator that records the command it is given and answers like the device.
"""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from hassil import recognize_best
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent

from custom_components.echomuse_schedule.intents import Handler, moved_item
from test_schedule import grammar

ZONE = ZoneInfo("Europe/Warsaw")
COPY = "sh-0123abcd-0123456789abcdef01234567"


def real_now():
    return datetime.now(ZONE)


def reminder(identity, days=2, state="scheduled", **over):
    due = real_now().replace(hour=23, minute=0, second=0, microsecond=0) + timedelta(days=days)
    return {"id": identity, "kind": "reminder", "label": identity, "message": identity, "state": state,
            "timezone": "Europe/Warsaw", "due": int(due.timestamp()), **over}


def alarm(identity, **over):
    return {"id": identity, "kind": "alarm", "label": "alarm", "state": "scheduled", "timezone": "Europe/Warsaw",
            "due": int(real_now().timestamp()) + 5000,
            "rule": {"days": [1, 2, 3, 4, 5], "interval": 0, "hour": 7, "minute": 0}, **over}


def say(tmp_path, phrase, items):
    """(speech, command sent or None, whether Assist matched an EchoMuse intent)."""
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.config.time_zone = "Europe/Warsaw"
        result = recognize_best(phrase, grammar(), language="en", best_metadata_key="custom_sentence")
        if result is None or not result.intent.name.startswith("EchoMuse"):
            await hass.async_stop(force=True)
            return None, None, False
        sent = []

        async def command(cmd):
            sent.append(cmd)
            if cmd["action"] == "edit":
                items[:] = [cmd["item"] if i["id"] == cmd["id"] else i for i in items]
            return {"snapshot": {"items": items}, "warning": None}

        coordinator = SimpleNamespace(device_id="source", last_update_success=True, last_created={},
                                      data={"active": "", "items": items}, command=command)
        intent.async_register(hass, Handler(result.intent.name, {"source": coordinator}))
        response = await intent.async_handle(hass, "conversation", result.intent.name,
            {name: {"value": entity.value} for name, entity in result.entities.items()},
            phrase, device_id="source")
        await hass.async_stop(force=True)
        return response.speech["plain"]["speech"], (sent[0] if sent else None), True
    return asyncio.run(run())


@pytest.mark.parametrize("phrase,hour,said", [
    ("move my reminder to 5pm tomorrow", 17, "Okay, your reminder is now at 5 PM tomorrow."),
    ("reschedule the reminder to tomorrow at 9am", 9, "Okay, your reminder is now at 9 AM tomorrow."),
    ("move the dentist reminder to 6:30 pm tomorrow", 18, "Okay, your reminder is now at 6:30 PM tomorrow."),
])
def test_moving_a_reminder(tmp_path, phrase, hour, said):
    items = [reminder("dentist")]
    speech, sent, matched = say(tmp_path, phrase, items)
    assert matched and speech == said
    assert sent["action"] == "edit" and sent["id"] == "dentist"
    moved = datetime.fromtimestamp(sent["item"]["due"], ZONE)
    assert moved.hour == hour and sent["item"]["message"] == "dentist"


def test_the_right_one_of_several_and_a_refusal_to_guess(tmp_path):
    items = [reminder("dentist"), reminder("bins", days=3)]
    speech, sent, _ = say(tmp_path, "move the bins reminder to 8am tomorrow", items)
    assert sent["id"] == "bins" and speech == "Okay, your reminder is now at 8 AM tomorrow."
    speech, sent, _ = say(tmp_path, "move my reminder to 5pm tomorrow", [reminder("a"), reminder("b")])
    assert sent is None and "Several items match" in speech


def test_a_repeating_alarm_keeps_its_days(tmp_path):
    items = [alarm("wake")]
    speech, sent, _ = say(tmp_path, "move my alarm to 8am", items)
    assert speech == "Okay, your alarm is now at 8 AM every weekday."
    assert sent["item"]["rule"] == {"days": [1, 2, 3, 4, 5], "interval": 0, "hour": 8, "minute": 0}
    assert sent["item"]["due"] == 0


@pytest.mark.parametrize("phrase,items,said", [
    ("move my reminder to 8", [reminder("a")], "Did you mean AM or PM?"),
    ("move my reminder to every weekday at 7am", [reminder("a")], "I can only move it to one time"),
    ("move my reminder to 5pm tomorrow", [reminder("a", state="completed")], "No matching item"),
    ("move my reminder to 5pm tomorrow", [reminder(COPY)], "shared from another Echo"),
])
def test_what_cannot_be_moved_is_said_and_nothing_is_sent(tmp_path, phrase, items, said):
    speech, sent, matched = say(tmp_path, phrase, items)
    assert matched and sent is None and said in speech


def test_moved_item_is_a_copy_and_refuses_the_past():
    current = reminder("a")
    now = real_now()
    moved = moved_item(current, now + timedelta(days=5), now.timestamp())
    assert moved is not current and current == reminder("a") | {"due": current["due"]}
    assert moved["due"] == int((now + timedelta(days=5)).timestamp())
    with pytest.raises(ValueError, match="already passed"):
        moved_item(current, now - timedelta(minutes=1), now.timestamp())
    with pytest.raises(ValueError, match="interval"):
        moved_item(alarm("x", rule={"days": [], "interval": 60, "hour": 0, "minute": 0}), now, now.timestamp())


@pytest.mark.parametrize("phrase", [
    "change my reminder to buy milk", "move the music to the kitchen", "move my timer to 5pm",
    "move my reminder", "reschedule", "what reminders do I have",
])
def test_sentences_that_are_not_a_move_are_not_claimed_as_one(tmp_path, phrase):
    result = recognize_best(phrase, grammar(), language="en", best_metadata_key="custom_sentence")
    assert result is None or not (result.intent.name == "EchoMuseAction" and result.entities.get("action")
                                  and result.entities["action"].value == "edit")


def test_cancel_my_reminder_names_nobody_unless_one_is_ringing(tmp_path):
    items = [reminder("dentist")]
    speech, sent, _ = say(tmp_path, "cancel my reminder", items)
    assert speech == "Reminder cancelled." and sent["id"] == "dentist"
    speech, sent, _ = say(tmp_path, "cancel my reminder", [reminder("a"), reminder("b")])
    assert sent is None and "Several items match" in speech
    speech, sent, _ = say(tmp_path, "cancel the bins reminder", [reminder("a"), reminder("bins", days=3)])
    assert sent["id"] == "bins"
