import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo
import pytest
from homeassistant.core import HomeAssistant, Context, SupportsResponse
from homeassistant.helpers import intent
from custom_components.echomuse_schedule.language import parse_item, Clarification
from custom_components.echomuse_schedule.dialogue import Dialogues
from custom_components.echomuse_schedule.intents import Handler
from custom_components.echomuse_schedule.tests.test_schedule import grammar
from hassil import recognize_best

NOW = datetime(2026,10,16,8,tzinfo=ZoneInfo('Europe/Warsaw'))

@pytest.mark.parametrize('text,hour,minute', [
 ('11.30 a.m.',11,30), ('11.30a.m.',11,30), ('11:30 am',11,30),
 ('11.30 p.m.',23,30), ('tomorrow at midday',12,0), ('tomorrow at noon',12,0),
 ('tomorrow at eight minutes past midnight',0,8), ('tomorrow at quarter to noon',11,45),
 ('tomorrow at half past eight in the morning',8,30),
])
def test_natural_times(text,hour,minute):
 item=parse_item('alarm',text,'Europe/Warsaw',NOW)
 due=datetime.fromtimestamp(item['due'],ZoneInfo('Europe/Warsaw'))
 assert (due.hour,due.minute)==(hour,minute)
 if 'tomorrow' in text: assert due.day==17


def test_exact_stt_through_real_grammar():
 r=recognize_best('Set an alarm for 11.30 a.m.',grammar(),language='en',best_metadata_key='custom_sentence')
 assert r.intent.name=='EchoMuseCreate'
 assert datetime.fromtimestamp(parse_item('alarm',r.entities['request'].value,'Europe/Warsaw',NOW)['due'],ZoneInfo('Europe/Warsaw')).hour==11

@pytest.mark.parametrize('phrase,answers,hour', [('tomorrow',['8am'],8),('tomorrow at eight',['pm'],20),('tomorrow',['eight','a.m.'],8)])
def test_native_question_flow_preserves_day_and_llm(tmp_path,phrase,answers,hour):
 async def run():
  hass=HomeAssistant(str(tmp_path));hass.config.time_zone='Europe/Warsaw'
  entity='assist_satellite.kitchen'
  attrs={'supported_features':3}
  hass.states.async_set(entity,'processing',attrs)
  prompts=[];announcements=[];commands=[]
  async def ask(call):
   assert call.data['entity_id']==entity
   assert not commands
   prompts.append(call.data['question'])
   return {'sentence':answers[len(prompts)-1]}
  async def announce(call): announcements.append(call.data['message'])
  hass.services.async_register('assist_satellite','ask_question',ask,supports_response=SupportsResponse.ONLY)
  hass.services.async_register('assist_satellite','announce',announce)
  c=SimpleNamespace(device_id='device-a',hass=hass,last_update_success=True,data={'items':[]})
  async def command(value):
   commands.append(value)
   item=value['item'];c.data={'items':[item]}
   return {'snapshot':c.data}
  c.command=command
  manager=Dialogues(hass)
  handler=Handler('EchoMuseCreate',{'a':c},dialogues=manager)
  obj=intent.Intent(hass,'conversation','EchoMuseCreate',{'kind':{'value':'alarm'},'request':{'value':phrase}},None,Context(),'en',device_id='device-a',conversation_agent_id='conversation.my_llm')
  record=SimpleNamespace(domain='assist_satellite',platform='esphome',disabled=False,entity_id=entity)
  # Registry lookup remains scoped to the source coordinator's device ID.
  with patch('custom_components.echomuse_schedule.dialogue.er.async_get',return_value=object()),patch('custom_components.echomuse_schedule.dialogue.er.async_entries_for_device',return_value=[record]) as lookup:
   response=await handler.async_handle(obj)
   assert response.speech['plain']['speech']==''
   assert not commands
   task=manager.tasks['device-a']
   await asyncio.sleep(0)
   assert not prompts  # initial pipeline has not released the satellite
   hass.states.async_set(entity,'idle',attrs)
   await asyncio.wait_for(task,3)
   assert len(prompts)==len(answers)
   assert len(commands)==1
   due=datetime.fromtimestamp(commands[0]['item']['due'],ZoneInfo('Europe/Warsaw'))
   assert due.hour==hour
   # Reference is captured at first request, not recomputed after clarification.
   assert due.date()==datetime.now(ZoneInfo('Europe/Warsaw')).date()+timedelta(days=1)
   assert announcements and obj.conversation_agent_id=='conversation.my_llm'
   lookup.assert_called_once_with(lookup.call_args.args[0],'device-a')
  await hass.async_stop(force=True)
 asyncio.run(run())


def test_cancel_followup_never_saves(tmp_path):
 async def run():
  hass=HomeAssistant(str(tmp_path));entity='assist_satellite.kitchen'
  hass.states.async_set(entity,'idle')
  async def ask(call): return {'sentence':'never mind'}
  async def announce(call): pass
  hass.services.async_register('assist_satellite','ask_question',ask,supports_response=SupportsResponse.ONLY)
  hass.services.async_register('assist_satellite','announce',announce)
  c=SimpleNamespace(hass=hass)
  await Dialogues(hass)._run(None,entity,c,'alarm','tomorrow',None,Clarification('What time?','time'),NOW,Context())
  await hass.async_stop(force=True)
 asyncio.run(run())


@pytest.mark.parametrize('fail', [False, True])
def test_dialogue_releases_controller_hold_on_no_answer_or_error(tmp_path, fail):
 async def run():
  from unittest.mock import AsyncMock
  from homeassistant.exceptions import HomeAssistantError
  hass=HomeAssistant(str(tmp_path));entity='assist_satellite.kitchen'
  hass.states.async_set(entity,'idle')
  api=SimpleNamespace(command=AsyncMock())
  c=SimpleNamespace(hass=hass,api=api)
  async def ask(call):
   assert api.command.await_args.args[0]['on'] is True
   if fail: raise HomeAssistantError('unavailable')
   return {'sentence':''}
  hass.services.async_register('assist_satellite','ask_question',ask,supports_response=SupportsResponse.ONLY)
  await Dialogues(hass)._run(None,entity,c,'reminder','tomorrow',None,Clarification('What time?','time'),NOW,Context())
  calls=[call.args[0] for call in api.command.await_args_list]
  assert [call['on'] for call in calls]==[True,False]
  assert calls[0]['token']==calls[1]['token']
  await hass.async_stop(force=True)
 asyncio.run(run())
