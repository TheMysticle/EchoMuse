"""English reminder wording through parsing and native clarification."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from hassil import recognize_best
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import intent

from custom_components.echomuse_schedule.dialogue import Dialogues, complete_request
from custom_components.echomuse_schedule.intents import Handler
from custom_components.echomuse_schedule.language import Clarification, parse_item
from test_schedule import FakeCoordinator, NOW, grammar


@pytest.mark.parametrize("phrase", [
    "to take out the trash at 8am",
    "at 8am to to take out the trash",
    "for tomorrow at 8am to take out the trash",
    "To take out the trash every two hours",
    "  to take out the trash every two hours",
    "for to take out the trash in five minutes",
])
def test_command_prepositions_are_not_saved_in_reminder_text(phrase):
    item = parse_item("reminder", phrase, "Europe/Warsaw", NOW)
    assert item["message"].lower() == "take out the trash"
    assert item["label"] == item["message"]


@pytest.mark.parametrize("phrase", ["every two hours", "to every two hours", "for every two hours"])
def test_interval_reminder_requires_a_message(phrase):
    with pytest.raises(Clarification) as missing:
        parse_item("reminder", phrase, "Europe/Warsaw", NOW)
    assert missing.value.field == "message"


def test_message_followup_does_not_add_a_second_to():
    assert complete_request("at 8am", "To take out the trash!", "message") == "to take out the trash at 8am"


@pytest.mark.parametrize("phrase", [
    "to drive to the station in five minutes",
    "to drive to the station every two hours",
    "to drive to the station at 8am",
])
def test_prepositions_inside_the_task_are_preserved(phrase):
    assert parse_item("reminder", phrase, "Europe/Warsaw", NOW)["message"] == "drive to the station"


@pytest.mark.parametrize("text,answers", [
    ("remind me to take out the trash", ["8am"]),
    ("remind me at 8am", ["to take out the trash"]),
    ("set a reminder for tomorrow at 8am", ["to take out the trash"]),
    ("set a reminder for the 26th of October", ["8am", "to take out the trash"]),
    ("remind me every two hours", ["to take out the trash"]),
    ("remind me To take out the trash every two hours", []),
    ("in five minutes remind me to take out the trash", []),
])
def test_real_english_sentences_and_followups_save_only_the_task(tmp_path, text, answers):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.config.time_zone = "Europe/Warsaw"
        entity_id = "assist_satellite.kitchen"
        hass.states.async_set(entity_id, "idle", {"supported_features": 3})
        prompts, announcements = [], []

        async def ask(call):
            prompts.append(call.data["question"])
            return {"sentence": answers[len(prompts)-1]}

        async def announce(call):
            announcements.append(call.data["message"])

        hass.services.async_register("assist_satellite", "ask_question", ask, supports_response=SupportsResponse.ONLY)
        hass.services.async_register("assist_satellite", "announce", announce)
        coordinator = FakeCoordinator()
        coordinator.hass = hass
        dialogues = Dialogues(hass)
        intent.async_register(hass, Handler("EchoMuseCreate", {"a": coordinator}, dialogues=dialogues))
        result = recognize_best(text, grammar(), language="en", best_metadata_key="custom_sentence")
        assert result is not None and result.intent.name == "EchoMuseCreate"
        slots = {name: {"value": entity.value} for name, entity in result.entities.items()}
        record = SimpleNamespace(domain="assist_satellite", platform="esphome", disabled=False, entity_id=entity_id)
        with patch("custom_components.echomuse_schedule.dialogue.er.async_get", return_value=object()), patch(
            "custom_components.echomuse_schedule.dialogue.er.async_entries_for_device", return_value=[record]
        ):
            response = await intent.async_handle(hass, "conversation", "EchoMuseCreate", slots, text, device_id="device-a")
            assert response.error_code is None, response.as_dict()
            if task := dialogues.tasks.get("device-a"):
                await asyncio.wait_for(task, 3)
        assert len(prompts) == len(answers)
        assert len(coordinator.commands) == 1
        item = coordinator.commands[0]["item"]
        assert item["message"].lower() == "take out the trash"
        assert item["label"] == item["message"]
        confirmation = announcements[-1] if announcements else response.speech["plain"]["speech"]
        assert "remind you to take out the trash" in confirmation.lower()
        await hass.async_stop(force=True)

    asyncio.run(run())


@pytest.mark.parametrize('phrase', ['for 9:40pm today', '9:40pm today', 'for 9:40 p.m. today', 'for 9:40pm today.'])
def test_supplied_clock_asks_for_message_not_time(phrase):
    with pytest.raises(Clarification) as missing:
        parse_item('reminder', phrase, 'Europe/Warsaw', NOW)
    assert missing.value.field == 'message'


@pytest.mark.parametrize('answer', ['9:40pm', 'at 9:40pm today', 'nine forty pm', '9:40 p.m.!'])
def test_repeated_clock_is_not_accepted_as_reminder_text(answer):
    with pytest.raises(Clarification) as missing:
        complete_request('for 9:40pm today', answer, 'message')
    assert missing.value.field == 'message'


@pytest.mark.parametrize('text', [
    'set a reminder for 9:40pm today',
    'set a reminder for 9:40 p.m. today',
    'set a reminder to take out the trash for 9:40pm today',
])
@pytest.mark.parametrize('answers,save', [
    (['to take out the trash'], True),
    (['9:40pm', 'to take out the trash'], True),
    (['9:40pm'] * 4, False),
    (['stop'], False),
    ([''], False),
])
def test_supplied_time_survives_real_dialogue_until_task_is_provided(tmp_path, text, answers, save):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.config.time_zone = 'Europe/Warsaw'
        entity_id = 'assist_satellite.kitchen'
        hass.states.async_set(entity_id, 'idle', {'supported_features': 3})
        has_message = 'take out' in text
        prompts, announcements = [], []
        coordinator = FakeCoordinator()
        coordinator.hass = hass

        async def ask(call):
            assert not coordinator.commands  # never save a time as a task
            assert call.data['question'] == 'What should I remind you to do?'
            prompts.append(call.data['question'])
            return {'sentence': answers[len(prompts)-1]}

        async def announce(call):
            announcements.append(call.data['message'])

        hass.services.async_register('assist_satellite', 'ask_question', ask, supports_response=SupportsResponse.ONLY)
        hass.services.async_register('assist_satellite', 'announce', announce)
        dialogues = Dialogues(hass)
        intent.async_register(hass, Handler('EchoMuseCreate', {'a': coordinator}, dialogues=dialogues))
        result = recognize_best(text, grammar(), language='en', best_metadata_key='custom_sentence')
        assert result is not None and result.intent.name == 'EchoMuseCreate'
        slots = {name: {'value': entity.value} for name, entity in result.entities.items()}
        record = SimpleNamespace(domain='assist_satellite', platform='esphome', disabled=False, entity_id=entity_id)
        with patch('custom_components.echomuse_schedule.intents.datetime') as clock, patch(
            'custom_components.echomuse_schedule.dialogue.er.async_get', return_value=object()
        ), patch('custom_components.echomuse_schedule.dialogue.er.async_entries_for_device', return_value=[record]):
            clock.now.return_value = NOW
            response = await intent.async_handle(hass, 'conversation', 'EchoMuseCreate', slots, text, device_id='device-a')
            assert response.error_code is None, response.as_dict()
            if task := dialogues.tasks.get('device-a'):
                await asyncio.wait_for(task, 3)
        assert len(prompts) == (0 if has_message else len(answers))
        if not (has_message or save):
            assert not coordinator.commands
            if len(answers) == 4:
                assert 'with the reminder text' in announcements[-1]
            await hass.async_stop(force=True)
            return
        assert len(coordinator.commands) == 1
        item = coordinator.commands[0]['item']
        assert item['message'] == item['label'] == 'take out the trash'
        expected = NOW.replace(hour=21, minute=40)
        assert item['due'] == int(expected.timestamp())
        await hass.async_stop(force=True)

    asyncio.run(run())
