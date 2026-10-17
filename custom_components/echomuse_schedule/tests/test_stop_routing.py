"""Bare stop must preserve native media handling outside the source's alert."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from hassil import recognize_best
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import intent

from custom_components.echomuse_schedule.dialogue import created_speech
from custom_components.echomuse_schedule.intents import MEDIA_PAUSE, MediaStopHandler
from test_schedule import grammar


@pytest.mark.parametrize('text', ['stop', 'stop!', 'Stop.', 'stop playing music', 'stop the music'])
def test_stop_uses_native_media_sentence(text):
    result = recognize_best(text, grammar(), language='en', best_metadata_key='custom_sentence',
        intent_context={'area': {'value': 'Kitchen', 'text': 'Kitchen'}})
    assert result is not None and result.intent.name == MEDIA_PAUSE
    assert not (result.intent_metadata or {}).get('custom_sentence')


@pytest.mark.parametrize('text,device,state,online,active,expected', [
    ('stop', 'a', 'ringing', True, 'alert', True),
    ('Please stop!', 'a', 'ringing', True, 'alert', True),
    ('can you stop', 'a', 'ringing', True, 'alert', True),
    ('could you stop for me', 'a', 'ringing', True, 'alert', True),
    ('would you stop please', 'a', 'ringing', True, 'alert', True),
    ('stop', 'a', 'scheduled', True, 'alert', False),
    ('stop', 'a', 'snoozed', True, 'alert', False),
    ('stop', 'a', 'ringing', True, '', False),
    ('stop', 'a', 'ringing', False, 'alert', False),
    ('stop', 'b', 'ringing', True, 'alert', False),
    ('stop', None, 'ringing', True, 'alert', False),
    ('stop the music', 'a', 'ringing', True, 'alert', False),
    ('pause music', 'a', 'ringing', True, 'alert', False),
    (None, 'a', 'ringing', True, 'alert', False),
    ('could you stop the music for me', 'a', 'ringing', True, 'alert', False),
    ('stop playing', 'a', 'ringing', True, 'alert', False),
])
def test_native_stop_dispatch(tmp_path, text, device, state, online, active, expected):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        original = SimpleNamespace(intent_type=MEDIA_PAUSE, description='Native media pause', slot_schema=None,
            async_handle=AsyncMock(return_value='original media response'))
        c = SimpleNamespace(device_id='a', last_update_success=online,
            data={'active': active, 'items': [{'id':'alert', 'kind':'alarm', 'state':state}]},
            async_refresh=AsyncMock(), command=AsyncMock())
        handler = MediaStopHandler(original, {'a':c})
        intent.async_register(hass, handler)
        result = await intent.async_handle(hass, 'conversation', MEDIA_PAUSE, {}, text, device_id=device)
        if expected:
            assert result.speech['plain']['speech'] == 'Alarm stopped.'
            c.command.assert_awaited_once()
            assert c.command.call_args.args[0]['id'] == 'alert'
            original.async_handle.assert_not_awaited()
        else:
            assert result == 'original media response'
            original.async_handle.assert_awaited_once()
            c.command.assert_not_awaited()
        await hass.async_stop(force=True)
    asyncio.run(run())


def test_stale_snapshot_refreshed_and_failed_dismiss_does_not_stop_music(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        original = SimpleNamespace(intent_type=MEDIA_PAUSE, description='Native', slot_schema=None,
            async_handle=AsyncMock(return_value='music'))
        c = SimpleNamespace(device_id='a', last_update_success=True, data={'active':'', 'items':[]},
            command=AsyncMock(side_effect=HomeAssistantError('offline')))
        async def refresh():
            c.data = {'active':'new', 'items':[{'id':'new', 'kind':'timer', 'state':'ringing'}]}
        c.async_refresh=refresh
        intent.async_register(hass, MediaStopHandler(original, {'a':c}))
        result = await intent.async_handle(hass, 'conversation', MEDIA_PAUSE, {}, 'stop', device_id='a')
        assert result.error_code is not None
        c.command.assert_awaited_once()
        original.async_handle.assert_not_awaited()
        await hass.async_stop(force=True)
    asyncio.run(run())


@pytest.mark.parametrize('kind,extra,expected', [
    ('timer', {'duration': 300}, 'Your timer is set for 5 minutes.'),
    ('alarm', {}, 'Your alarm is set at 8 AM tomorrow.'),
    ('alarm', {'rule':{'days':[1,2,3,4,5], 'hour':8, 'minute':0}}, 'Your alarm is set at 8 AM every weekday.'),
    ('reminder', {'message':'take medicine'}, "Okay, I'll remind you to take medicine at 8 AM tomorrow."),
])
def test_natural_confirmation(kind, extra, expected):
    zone = ZoneInfo('Europe/Warsaw')
    due = (datetime.now(zone)+timedelta(days=1)).replace(hour=8, minute=0, second=0)
    item = {'id':'one', 'kind':kind, 'label':kind, 'timezone':str(zone), 'due':int(due.timestamp()), **extra}
    assert created_speech(kind, item, {'snapshot':{'items':[item]}}) == expected


@pytest.mark.parametrize('text', [
    'play alarm', 'play the timer', 'play the alarm', 'play remind me by Royksopp',
    'arm the alarm', 'disarm the alarm', 'set the home alarm', 'turn off the alarm',
    'set the alarm to armed away', 'stop the blinds', 'stop the vacuum',
    'pause music', 'resume', 'next track', 'previous track', 'mute music',
])
def test_schedule_sentences_do_not_capture_unrelated_commands(text):
    result = recognize_best(text, grammar(), language='en', best_metadata_key='custom_sentence',
        intent_context={'area': {'value':'Kitchen', 'text':'Kitchen'}})
    assert result is None or not result.intent.name.startswith('EchoMuse')


@pytest.mark.parametrize('text,kind,message', [
    ('at 8am remind me to take medicine', 'reminder', 'take medicine'),
    ('tomorrow remind me to take medicine at 8am', 'reminder', 'take medicine'),
    ('every weekday at 8am remind me to take medicine', 'reminder', 'take medicine'),
    ('in five minutes remind me to take medicine', 'reminder', 'take medicine'),
    ('set an alarm at 8am', 'alarm', None),
    ('set an alarm tomorrow at 8am', 'alarm', None),
    ('set an alarm every weekday at 8am', 'alarm', None),
    ('a five minute timer', 'timer', None),
    ('an 8am alarm', 'alarm', None),
])
def test_temporal_introducers_preserve_schedule_requests(tmp_path, text, kind, message):
    from custom_components.echomuse_schedule.intents import Handler
    from test_schedule import FakeCoordinator

    async def run():
        hass = HomeAssistant(str(tmp_path))
        hass.config.time_zone = 'Europe/Warsaw'
        result = recognize_best(text, grammar(), language='en', best_metadata_key='custom_sentence')
        assert result is not None and result.intent.name == 'EchoMuseCreate'
        c = FakeCoordinator()
        intent.async_register(hass, Handler('EchoMuseCreate', {'a':c}))
        slots = {name:{'value':entity.value} for name,entity in result.entities.items()}
        response = await intent.async_handle(hass,'conversation','EchoMuseCreate',slots,text,device_id='device-a')
        assert response.error_code is None, response.as_dict()
        item = c.commands[-1]['item']
        assert item['kind'] == kind
        if message:
            assert item['message'] == message
        if 'weekday' in text:
            assert item['rule'] == {'days':[1,2,3,4,5], 'hour':8, 'minute':0}
        elif 'five minute' in text:
            assert item['duration'] == 300
        else:
            assert datetime.fromtimestamp(item['due'],ZoneInfo('Europe/Warsaw')).hour == 8
        await hass.async_stop(force=True)
    asyncio.run(run())


def test_repeated_stop_preserves_music_but_explicit_music_command_passes(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        original = SimpleNamespace(intent_type=MEDIA_PAUSE, description='Native', slot_schema=None,
            async_handle=AsyncMock(return_value='music'))
        c = SimpleNamespace(device_id='a', last_update_success=True,
            data={'active':'alert','items':[{'id':'alert','kind':'reminder','state':'ringing'}]},
            async_refresh=AsyncMock(), command=AsyncMock())
        intent.async_register(hass, MediaStopHandler(original, {'a':c}))
        await intent.async_handle(hass,'conversation',MEDIA_PAUSE,{},'stop!',device_id='a')
        c.data = {'active':'','items':[]}
        result = await intent.async_handle(hass,'conversation',MEDIA_PAUSE,{},'stop!',device_id='a')
        assert result.speech['plain']['speech'] == 'The alert is already stopped.'
        original.async_handle.assert_not_awaited()
        c.command.assert_awaited_once()
        assert await intent.async_handle(hass,'conversation',MEDIA_PAUSE,{},'stop the music!',device_id='a') == 'music'
        c.alert_stop_until = 0
        assert await intent.async_handle(hass,'conversation',MEDIA_PAUSE,{},'stop',device_id='a') == 'music'
        await hass.async_stop(force=True)
    asyncio.run(run())
