"""Calendar reminders and deletion use the real grammar and source-scoped data."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest
from hassil import recognize_best
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent

from custom_components.echomuse_schedule.coordinator import Coordinator
from custom_components.echomuse_schedule.dialogue import complete_request, created_speech
from custom_components.echomuse_schedule.intents import Handler, select
from custom_components.echomuse_schedule.language import parse_item
from test_schedule import grammar

ZONE = ZoneInfo("Europe/Warsaw")
NOW = datetime(2026, 10, 16, 8, tzinfo=ZONE)


@pytest.mark.parametrize("day", ["20th", "twentieth", "20"])
@pytest.mark.parametrize("clock", ["6 p.m.", "six pm", "18:00"])
def test_calendar_clause_is_a_date_not_reminder_text(day, clock):
    text = f"remind me to take the trash out on the {day} at {clock}"
    result = recognize_best(text, grammar(), language="en", best_metadata_key="custom_sentence")
    assert result.intent.name == "EchoMuseCreate"
    item = parse_item("reminder", result.entities["request"].value, "Europe/Warsaw", NOW)
    assert datetime.fromtimestamp(item["due"], ZONE) == datetime(2026, 10, 20, 18, tzinfo=ZONE)
    assert item["message"] == item["label"] == "take the trash out"
    with patch("custom_components.echomuse_schedule.dialogue.datetime") as clock_now:
        clock_now.now.return_value = NOW
        clock_now.fromtimestamp.side_effect = datetime.fromtimestamp
        speech = created_speech("reminder", item, {"snapshot": {"items": [{**item, "id": "one"}]}})
    assert "20 October" in speech
    assert "today" not in speech


@pytest.mark.parametrize("now,day,expected", [
    ("2026-10-20T08:00", "20th", "2026-10-20T18:00"),
    ("2026-10-20T18:01", "20th", "2026-11-20T18:00"),
    ("2026-12-31T19:00", "31st", "2027-01-31T18:00"),
    ("2026-04-30T08:00", "31st", "2026-05-31T18:00"),
    ("2027-02-28T19:00", "29th", "2027-03-29T18:00"),
    ("2028-02-28T08:00", "twenty-ninth", "2028-02-29T18:00"),
    ("2026-10-16T08:00", "twenty fifth", "2026-10-25T18:00"),
])
def test_next_day_of_month_handles_rollover_leap_year_and_dst(now, day, expected):
    reference = datetime.fromisoformat(now).replace(tzinfo=ZONE)
    item = parse_item("reminder", f"to call Sam on the {day} at 6pm", "Europe/Warsaw", reference)
    assert datetime.fromtimestamp(item["due"], ZONE) == datetime.fromisoformat(expected).replace(tzinfo=ZONE)


@pytest.mark.parametrize("text", [
    "to call Sam on the 0th at 6pm", "to call Sam on the 32nd at 6pm",
    "to call Sam on the thirty-second at 6pm",
    "to call Sam on the 20th tomorrow at 6pm",
    "to call Sam on the 20th every day at 6pm",
    "to call Sam on the 20th in five minutes",
    "to call Sam on the 20th on the 21st at 6pm",
    "to call Sam on the 31st of April at 6pm",
])
def test_invalid_or_conflicting_dates_never_fall_back_to_today(text):
    with pytest.raises(ValueError):
        parse_item("reminder", text, "Europe/Warsaw", NOW)


@pytest.mark.parametrize("answer", ["pm", "6pm", "at 6pm"])
def test_clock_clarification_preserves_day_and_number_in_message(answer):
    request = "to buy 2 bags on the 20th at six"
    completed = complete_request(request, answer, "meridian")
    item = parse_item("reminder", completed, "Europe/Warsaw", NOW)
    assert item["message"] == "buy 2 bags"
    assert datetime.fromtimestamp(item["due"], ZONE) == datetime(2026, 10, 20, 18, tzinfo=ZONE)


def test_nonexistent_dst_clock_is_not_silently_changed():
    reference = datetime(2027, 3, 1, 8, tzinfo=ZONE)
    with pytest.raises(ValueError, match="clocks change"):
        parse_item("reminder", "to call Sam on the 28th at 2:30am", "Europe/Warsaw", reference)


def test_ordinal_in_task_is_not_mistaken_for_a_date():
    item = parse_item("reminder", "to leave the box on the third floor at 6pm", "Europe/Warsaw", NOW)
    assert datetime.fromtimestamp(item["due"], ZONE).date() == NOW.date()
    assert "third floor" in item["message"]


def reminder(identity, days=0, state="scheduled"):
    return {"id": identity, "kind": "reminder", "label": identity, "message": identity,
            "state": state, "timezone": "Europe/Warsaw",
            "due": int((NOW.replace(hour=18) + timedelta(days=days)).timestamp())}


@pytest.mark.parametrize("target,expected", [
    ("for 6pm today", "today"), ("at 6 p.m. tomorrow", "tomorrow"),
    ("6pm on the 20th", "twentieth"), ("that", "twentieth"),
    ("on the 20th at 6pm", "twentieth"),
])
def test_delete_selects_date_or_last_created_without_needing_a_ringing_item(target, expected):
    data = {"active": "", "items": [reminder("today"), reminder("tomorrow", 1), reminder("twentieth", 4)]}
    assert select(data, "reminder", target, recent={"reminder": "twentieth"}, now=NOW)["id"] == expected


def test_ambiguous_or_stale_reference_never_deletes_a_different_reminder():
    data = {"active": "", "items": [reminder("today"), reminder("tomorrow", 1)]}
    for target, recent in [("that", {}), ("6pm", {}), ("that", {"reminder": "removed"})]:
        with pytest.raises(ValueError):
            select(data, "reminder", target, recent=recent, now=NOW)
    data["items"] = [reminder("only")]
    assert select(data, "reminder", "that")["id"] == "only"
    data["items"][0]["state"] = "canceled"
    with pytest.raises(ValueError):
        select(data, "reminder", "that", recent={"reminder": "only"})


@pytest.mark.parametrize("target", ["for 6pm next month", "for 6pm on the 20th of May", "6pm today tomorrow"])
def test_unsupported_delete_date_is_not_ignored(target):
    with pytest.raises(ValueError):
        select({"active": "", "items": [reminder("today")]}, "reminder", target, now=NOW)


@pytest.mark.parametrize("phrase,expected", [
    ("delete that reminder", "latest"),
    ("delete my reminder for 6pm today", "today"),
])
def test_delete_through_real_sentence_and_ha_handler(tmp_path, phrase, expected):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.config.time_zone = "Europe/Warsaw"
        result = recognize_best(phrase, grammar(), language="en", best_metadata_key="custom_sentence")
        assert result.intent.name == "EchoMuseAction"
        coordinator = SimpleNamespace(device_id="source", last_update_success=True,
            last_created={"reminder": "latest"},
            data={"active": "", "items": [reminder("today"), reminder("latest", 4)]},
            command=AsyncMock(return_value={"snapshot": {"items": []}}))
        intent.async_register(hass, Handler("EchoMuseAction", {"source": coordinator}))
        with patch("custom_components.echomuse_schedule.intents.datetime") as clock:
            clock.now.return_value = NOW
            clock.fromtimestamp.side_effect = datetime.fromtimestamp
            response = await intent.async_handle(hass, "conversation", "EchoMuseAction",
                {name: {"value": entity.value} for name, entity in result.entities.items()},
                phrase, device_id="source")
        assert response.error_code is None
        assert coordinator.command.call_args.args[0]["id"] == expected
        await hass.async_stop(force=True)
    asyncio.run(run())


def test_recent_reference_is_recorded_only_after_a_successful_create(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        coordinator = object.__new__(Coordinator)
        coordinator.hass = hass
        coordinator.data = {"items": []}
        coordinator.command_lock = asyncio.Lock()
        coordinator.last_created = {}
        coordinator.api = SimpleNamespace(command=AsyncMock(return_value={"items": []}))
        coordinator.async_set_updated_data = Mock()
        coordinator.entry = SimpleNamespace(data={})
        await coordinator.command({"action": "create", "item": {**reminder("new"), "speech": "cached"}})
        assert coordinator.last_created == {"reminder": "new"}
        coordinator.api.command.side_effect = ValueError("save failed")
        with pytest.raises(ValueError):
            await coordinator.command({"action": "create", "item": {**reminder("failed"), "speech": "cached"}})
        assert coordinator.last_created == {"reminder": "new"}
        await hass.async_stop(force=True)
    asyncio.run(run())


@pytest.mark.parametrize("text", ["Set a reminder for the 26th of October", "set my reminder for the twenty-sixth of October"])
def test_named_month_request_clarifies_locally(text):
    from custom_components.echomuse_schedule.language import Clarification
    result = recognize_best(text, grammar(), language="en", best_metadata_key="custom_sentence")
    assert result is not None and result.intent.name == "EchoMuseCreate"
    request = result.entities["request"].value
    with pytest.raises(Clarification) as missing:
        parse_item("reminder", request, "Europe/Warsaw", NOW)
    assert missing.value.field == "time"
    request = complete_request(request, "6pm", "time")
    with pytest.raises(Clarification) as missing:
        parse_item("reminder", request, "Europe/Warsaw", NOW)
    assert missing.value.field == "message"
    item = parse_item("reminder", complete_request(request, "call Sam", "message"), "Europe/Warsaw", NOW)
    assert item["message"] == "call sam"
    assert datetime.fromtimestamp(item["due"], ZONE) == datetime(2026, 10, 26, 18, tzinfo=ZONE)


@pytest.mark.parametrize("text,expected", [
    ("to call Sam on the 20th of May at 6pm", datetime(2027,5,20,18,tzinfo=ZONE)),
    ("to call Sam on the 26th of October 2027 at 6pm", datetime(2027,10,26,18,tzinfo=ZONE)),
])
def test_named_month_keeps_exact_date(text, expected):
    item = parse_item("reminder", text, "Europe/Warsaw", NOW)
    assert datetime.fromtimestamp(item["due"], ZONE) == expected
    assert item["message"] == "call sam"


def test_legacy_cached_speech_is_regenerated_with_reminder_intro(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path));hass.config.time_zone='Europe/Warsaw'
        c = object.__new__(Coordinator)
        c.hass = hass;c.entry = SimpleNamespace(data={})
        c.command_lock = asyncio.Lock();c.data={'items':[]};c.last_created={}
        c.api=SimpleNamespace(command=AsyncMock(return_value={'items':[]}),asset=AsyncMock(return_value='new-audio'))
        c.async_set_updated_data=Mock()
        with patch('custom_components.echomuse_schedule.coordinator.generate_media_source_id', return_value='media-source') as generate, patch('custom_components.echomuse_schedule.coordinator.tts.async_get_media_source_audio', new=AsyncMock(return_value=('audio/wav',b'audio'))):
            await c.command({'action':'edit','item':{'id':'old','kind':'reminder','message':'call Sam','speech':'old-audio'}},cache_speech=True)
        assert generate.call_args.args[1] == "Here's your reminder. call Sam"
        saved=c.api.command.call_args.args[0]['item']
        assert saved['speech']=='new-audio' and saved['speech_version']==1
        await hass.async_stop(force=True)
    asyncio.run(run())
