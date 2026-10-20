"""Real authenticated HTTP MCP calls into the acknowledged schedule bridge."""
import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
import pytest

import em_api as api
import em_ingressauth
import em_state
import em_schedule_auth as keys
import em_workflow_tools as tools
from em_schedule import Bridge
import test_schedule_auth as auth_fixtures

database = auth_fixtures.database


NOW=int(datetime(2026,10,17,12,tzinfo=timezone.utc).timestamp())


@pytest.mark.parametrize('kind', ['timer','alarm','reminder'])
def test_create_maps_to_device_contract(kind):
    cmd=tools.command('EchoMuseCreate',{'kind':kind,'duration_seconds':300,'timezone':'Europe/Warsaw','message':'take bins out'},NOW)
    assert cmd['action']=='create'
    assert cmd['item']['due']==NOW+300 and cmd['item']['duration']==300
    assert cmd['item']['kind']==kind and cmd['item']['timezone']=='Europe/Warsaw'
    assert cmd['request'] and cmd['item']['id']


def test_calendar_recurrence_uses_existing_device_rule():
    item=tools.command('EchoMuseCreate',{'kind':'alarm','when':'2026-10-18T08:00:00+02:00','timezone':'Europe/Warsaw','repeat_days':[1,2,3,4,5]},NOW)['item']
    assert item['rule']=={'days':[1,2,3,4,5],'interval':0,'hour':8,'minute':0}


@pytest.mark.parametrize('change', [
    {'duration_seconds':True}, {'duration_seconds':-3}, {'kind':'security'},
    {'device_id':'B'}, {'kind':'reminder'}, {'timezone':'Bad/Zone'},
    {'when':'2026-10-18T08:00:00'}, {'when':'2026-10-18T08:00:00+01:00'},
    {'repeat_days':[True]}, {'repeat_days':[]}, {'repeat_days':[1],'repeat_seconds':60},
])
def test_ambiguous_invalid_and_redirected_creates_fail(change):
    args={'kind':'alarm','duration_seconds':300,'timezone':'Europe/Warsaw',**change}
    if 'when' in change: args.pop('duration_seconds')
    with pytest.raises((ValueError,KeyError)):
        tools.command('EchoMuseCreate',args,NOW)


def test_mcp_auth_source_scope_device_ack_and_failure(database):
    async def main():
        sent=[]
        bridge=Bridge('A')
        bridge.update({'owner':'A','revision':1,'items':[]})
        async def send(message):
            cmd=message['command']
            sent.append(cmd)
            items=bridge.snapshot['items']
            if cmd['action']=='create': items=[*items,cmd['item']]
            bridge.receive({'id':message['id'],'snapshot':{'owner':'A','revision':bridge.snapshot['revision']+1,'items':items}})
        live=SimpleNamespace(capabilities=['local_schedule'],schedule=bridge,send_control=send,workflow=SimpleNamespace(active=True,config={"local_schedules":True}))
        key=keys.issue('A',voice=True)
        companion=keys.issue('A')  # does not revoke the voice key
        assert keys._resolve(key)=='A' and keys._resolve(companion)=='A'
        with patch.object(em_state,'devices',{'A':live}), patch.object(em_ingressauth, 'INGRESS_ONLY',True), patch.object(em_ingressauth, 'INGRESS_GATEWAY_IP','172.30.32.2'):
            async with TestClient(TestServer(await api.create_app())) as client:
                headers={'Authorization':'Bearer '+key}
                url='/api/devices/A/voice-workflow/mcp'
                async def call(method,params=None,url=url,headers=headers):
                    return await client.post(url,headers=headers,json={'jsonrpc':'2.0','id':1,'method':method,'params':params or {}})
                response=await call('initialize')
                assert response.status==200
                assert (await response.json())['result']['capabilities']=={'tools':{}}
                response=await call('tools/list')
                assert {tool['name'] for tool in (await response.json())['result']['tools']}=={
                    'EchoMuseStop','EchoMuseVolume','EchoMuseCreate','EchoMuseList','EchoMuseAction'}
                response=await call('tools/call',{'name':'EchoMuseCreate','arguments':{'kind':'timer','duration_seconds':300,'timezone':'UTC'}})
                result=(await response.json())['result']
                assert result['isError'] is False
                assert json.loads(result['content'][0]['text'])['device_acknowledged']
                assert sent[-1]['owner']=='A' and sent[-1]['item']['duration']==300
                response=await call('tools/call',{'name':'EchoMuseAction','arguments':{'action':'snooze','id':sent[-1]['item']['id'],'seconds':600}})
                assert not (await response.json())['result']['isError']
                response=await call('tools/call',{'name':'EchoMuseCreate','arguments':{'kind':'timer','duration_seconds':300,'timezone':'UTC','device_id':'B'}})
                assert (await response.json())['result']['isError']
                assert (await call('tools/list',url='/api/devices/B/voice-workflow/mcp')).status==403
                assert (await client.get('/api/schedules',headers=headers)).status==403
                assert (await client.post('/api/devices/A/schedules',headers=headers,json={'action':'list'})).status==403
                assert (await call('tools/list',headers={})).status==403
                with patch.object(bridge,'request',side_effect=asyncio.TimeoutError):
                    response=await call('tools/call',{'name':'EchoMuseList','arguments':{}})
                    assert (await response.json())['result']['isError']
                live.workflow.config['local_schedules']=False
                response=await call('tools/list')
                assert [tool['name'] for tool in (await response.json())['result']['tools']]==['EchoMuseStop','EchoMuseVolume']
                live.workflow.active=False
                response=await call('tools/call',{'name':'EchoMuseList'})
                assert (await response.json())['result']['isError']
    asyncio.run(main())


def test_volume_sets_only_this_speaker_never_an_ha_area(database, monkeypatch):
    # "volume 20" with no named device must change only this speaker — never
    # go through a Home Assistant volume tool, which given only an area can
    # reach every matching media player there, a TV included.
    async def main():
        import em_esphome
        calls=[]
        monkeypatch.setattr(em_esphome,'command_device_volume',
            lambda device_id,volume: calls.append((device_id,volume)) or True)
        live=SimpleNamespace(device_id='A',capabilities=[],
            workflow=SimpleNamespace(active=True,config={}))
        key=keys.issue('A',voice=True)
        with patch.object(em_state,'devices',{'A':live}), patch.object(em_ingressauth, 'INGRESS_ONLY',True), patch.object(em_ingressauth, 'INGRESS_GATEWAY_IP','172.30.32.2'):
            async with TestClient(TestServer(await api.create_app())) as client:
                headers={'Authorization':'Bearer '+key}
                url='/api/devices/A/voice-workflow/mcp'
                async def call(method,params=None):
                    return await client.post(url,headers=headers,json={'jsonrpc':'2.0','id':1,'method':method,'params':params or {}})
                response=await call('tools/list')
                assert 'EchoMuseVolume' in [t['name'] for t in (await response.json())['result']['tools']]
                response=await call('tools/call',{'name':'EchoMuseVolume','arguments':{'level':20}})
                result=(await response.json())['result']
                assert result['isError'] is False
                assert json.loads(result['content'][0]['text'])=={'volume_set':20}
                assert calls==[('A',0.2)]
    asyncio.run(main())


@pytest.mark.parametrize('args',[{},{'level':150},{'level':-1},{'level':'20'},{'level':20,'area':'Kitchen'}])
def test_volume_refuses_anything_but_a_bare_percentage(database, args):
    async def main():
        live=SimpleNamespace(device_id='A',capabilities=[],
            workflow=SimpleNamespace(active=True,config={}))
        key=keys.issue('A',voice=True)
        with patch.object(em_state,'devices',{'A':live}), patch.object(em_ingressauth, 'INGRESS_ONLY',True), patch.object(em_ingressauth, 'INGRESS_GATEWAY_IP','172.30.32.2'):
            async with TestClient(TestServer(await api.create_app())) as client:
                headers={'Authorization':'Bearer '+key}
                url='/api/devices/A/voice-workflow/mcp'
                response=await client.post(url,headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'EchoMuseVolume','arguments':args}})
                assert (await response.json())['result']['isError']
    asyncio.run(main())


def test_volume_reports_a_disconnected_speaker_rather_than_claiming_success(database, monkeypatch):
    async def main():
        import em_esphome
        monkeypatch.setattr(em_esphome,'command_device_volume',lambda device_id,volume: False)
        live=SimpleNamespace(device_id='A',capabilities=[],
            workflow=SimpleNamespace(active=True,config={}))
        key=keys.issue('A',voice=True)
        with patch.object(em_state,'devices',{'A':live}), patch.object(em_ingressauth, 'INGRESS_ONLY',True), patch.object(em_ingressauth, 'INGRESS_GATEWAY_IP','172.30.32.2'):
            async with TestClient(TestServer(await api.create_app())) as client:
                headers={'Authorization':'Bearer '+key}
                url='/api/devices/A/voice-workflow/mcp'
                response=await client.post(url,headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'EchoMuseVolume','arguments':{'level':20}}})
                assert (await response.json())['result']['isError']
    asyncio.run(main())


def _stop_rig(monkeypatch, items, spotify='playing', stream=True, ha_ring=False):
    """A speaker with the given schedule items, Spotify state and HA stream."""
    from types import ModuleType
    from unittest.mock import AsyncMock

    bridge=Bridge('A')
    bridge.update({'owner':'A','revision':1,'items':[dict(i) for i in items]})
    sent=[]
    async def send(message):
        command=message['command']
        sent.append(command)
        bridge.receive({'id':message['id'],'snapshot':{
            'owner':'A','revision':bridge.snapshot['revision']+1,'items':bridge.snapshot['items']}})
    spotify_stop=AsyncMock(return_value={})
    player_stop=AsyncMock()
    em_player=ModuleType('em_player')
    em_player._sessions={'A':SimpleNamespace(state='playing' if stream else 'idle',resume_after=False)}
    em_player.PLAYING='playing'
    em_player.stop=player_stop
    monkeypatch.setitem(__import__('sys').modules,'em_player',em_player)
    live=SimpleNamespace(device_id='A',capabilities=['local_schedule'],schedule=bridge,send_control=send,
        spotify={'state':spotify},spotify_request=spotify_stop,
        stop_timer_alarm=AsyncMock(return_value=ha_ring))
    return live,sent,spotify_stop,player_stop


COUNTDOWN={'id':'tea','kind':'timer','label':'tea','state':'scheduled'}
RINGING_TIMER={'id':'eggs','kind':'timer','label':'eggs','state':'ringing'}
RINGING_ALARM={'id':'wake','kind':'alarm','label':'wake up','state':'ringing'}
LATER_ALARM={'id':'later','kind':'alarm','label':'tomorrow','state':'scheduled'}


def test_a_ringing_timer_is_stopped_and_the_music_is_left_playing(monkeypatch):
    # Seen: music playing and a timer going off, and one "stop" silenced both.
    async def main():
        live,sent,spotify_stop,player_stop=_stop_rig(monkeypatch,[RINGING_TIMER,COUNTDOWN])
        result=await tools._stop_active_local_audio(live,True)
        assert [(c['action'],c['id']) for c in sent]==[('cancel','eggs')]
        spotify_stop.assert_not_called(); player_stop.assert_not_called()
        assert [i['type'] for i in result['stopped']]==['timer']
        assert {i['type'] for i in result['left_running']}=={'spotify_connect','home_assistant_stream','timer'}
    asyncio.run(main())


def test_a_ringing_alarm_is_dismissed_not_cancelled(monkeypatch):
    async def main():
        live,sent,_,_=_stop_rig(monkeypatch,[RINGING_ALARM,LATER_ALARM],spotify='idle',stream=False)
        result=await tools._stop_active_local_audio(live,True)
        assert [(c['action'],c['id']) for c in sent]==[('dismiss','wake')]
        assert result['left_running']==[]
    asyncio.run(main())


def test_a_timer_ringing_from_the_controller_also_outranks_music(monkeypatch):
    async def main():
        live,sent,spotify_stop,player_stop=_stop_rig(monkeypatch,[],ha_ring=True)
        result=await tools._stop_active_local_audio(live,True)
        assert result['stopped']==[{'type':'ringing_timer'}]
        spotify_stop.assert_not_called(); player_stop.assert_not_called()
        assert {i['type'] for i in result['left_running']}=={'spotify_connect','home_assistant_stream'}
    asyncio.run(main())


def test_with_nothing_ringing_stop_ends_the_music_and_leaves_a_countdown_alone(monkeypatch):
    async def main():
        live,sent,spotify_stop,player_stop=_stop_rig(monkeypatch,[COUNTDOWN,LATER_ALARM])
        result=await tools._stop_active_local_audio(live,True)
        assert sent==[]
        spotify_stop.assert_awaited_once_with('stop',{}); player_stop.assert_awaited_once_with('A')
        assert [i['type'] for i in result['stopped']]==['spotify_connect','home_assistant_stream']
        assert result['left_running']==[{'type':'timer','id':'tea','label':'tea'}]
    asyncio.run(main())


def test_with_only_a_countdown_left_stop_cancels_it_but_never_a_later_alarm(monkeypatch):
    async def main():
        live,sent,_,_=_stop_rig(monkeypatch,[COUNTDOWN,LATER_ALARM],spotify='idle',stream=False)
        result=await tools._stop_active_local_audio(live,True)
        assert [(c['action'],c['id']) for c in sent]==[('cancel','tea')]
        assert [i['id'] for i in result['stopped']]==['tea']
    asyncio.run(main())


def test_a_second_stop_does_the_next_thing(monkeypatch):
    async def main():
        live,sent,spotify_stop,_=_stop_rig(monkeypatch,[RINGING_TIMER])
        await tools._stop_active_local_audio(live,True)
        live.schedule.snapshot['items'][:]=[]          # the ring is over
        again=await tools._stop_active_local_audio(live,True)
        spotify_stop.assert_awaited_once_with('stop',{})
        assert [i['type'] for i in again['stopped']]==['spotify_connect','home_assistant_stream']
    asyncio.run(main())


def test_local_schedules_off_still_stops_the_music(monkeypatch):
    async def main():
        live,sent,spotify_stop,player_stop=_stop_rig(monkeypatch,[RINGING_TIMER])
        result=await tools._stop_active_local_audio(live,False)
        assert sent==[]
        spotify_stop.assert_awaited_once_with('stop',{}); player_stop.assert_awaited_once_with('A')
    asyncio.run(main())


def test_reminder_text_given_only_as_the_label_is_the_message():
    # Seen live: three identical EchoMuseCreate calls with label "take out the
    # trash" and no message, each refused with "ask what to be reminded about".
    item=tools.command('EchoMuseCreate',{'kind':'reminder','duration_seconds':300,'label':'take out the trash','timezone':'Europe/Warsaw'},NOW)['item']
    assert item['message']=='take out the trash' and item['label']=='take out the trash'


def test_an_explicit_message_wins_over_the_label():
    item=tools.command('EchoMuseCreate',{'kind':'reminder','duration_seconds':300,'label':'bins','message':'take out the trash','timezone':'Europe/Warsaw'},NOW)['item']
    assert item['message']=='take out the trash' and item['label']=='bins'


@pytest.mark.parametrize('extra',[{},{'label':''},{'label':'  '},{'label':'reminder'},{'message':' '}])
def test_a_reminder_with_no_text_at_all_is_refused_with_a_way_forward(extra):
    with pytest.raises(ValueError) as error:
        tools.command('EchoMuseCreate',{'kind':'reminder','duration_seconds':300,'timezone':'Europe/Warsaw',**extra},NOW)
    # Tells the model what to do, not just that it failed.
    assert 'message' in str(error.value) and 'ask' in str(error.value)


def test_timers_and_alarms_still_need_no_message():
    for kind in ('timer','alarm'):
        item=tools.command('EchoMuseCreate',{'kind':kind,'duration_seconds':300,'label':'x','timezone':'Europe/Warsaw'},NOW)['item']
        assert item['message']==''


def test_the_tool_description_says_a_reminder_requires_message():
    spec=next(t for t in tools.TOOLS if t['name']=='EchoMuseCreate')
    assert 'REQUIRES `message`' in spec['description']


def test_list_day_view_filters_in_the_items_own_timezone():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    import em_workflow_tools as t
    zone = ZoneInfo("Europe/London")
    now = int(datetime(2026, 10, 20, 8, 0, tzinfo=zone).timestamp())

    def at(day, hour):
        return int(datetime(2026, 10, day, hour, 0, tzinfo=zone).timestamp())
    items = [{"id": "a", "due": at(20, 23), "timezone": "Europe/London"},
             {"id": "b", "due": at(21, 0), "timezone": "Europe/London"},
             {"id": "c", "due": at(22, 9), "timezone": "Europe/London"},
             {"id": "d", "due": 0, "timezone": "Europe/London"}]
    ids = lambda date: [i["id"] for i in t.day_view(items, date, now)]
    assert ids(None) == ["a", "b", "c", "d"]
    assert ids("today") == ["a"]
    assert ids("Tomorrow") == ["b"]
    assert ids("2026-10-22") == ["c"]
    assert t.day_view(items, None, now)[0]["due_local"].startswith("2026-10-20T23:00")
    assert t.day_view(items, None, now)[3]["due_local"] is None
    with pytest.raises(ValueError):
        t.day_view(items, "next week", now)
    assert t.command("EchoMuseList", {"kind": "reminder", "date": "today"}, now) == {"action": "list"}
