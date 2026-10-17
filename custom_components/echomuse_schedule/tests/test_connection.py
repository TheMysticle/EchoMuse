"""Real aiohttp requests and HA config-flow forms for satellite keys."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from custom_components.echomuse_schedule.api import API
from custom_components.echomuse_schedule.config_flow import ScheduleFlow


def test_key_requests_never_attempt_password_login():
    async def run():
        calls = []
        async def handle(request):
            calls.append((request.method, request.path, request.headers.get('Authorization')))
            if request.headers.get('Authorization') != 'Bearer satellite-key':
                raise web.HTTPForbidden(text='Bad key')
            if request.method == 'GET':
                return web.json_response([{'id':'A', 'mac':'02:00:00:00:00:01', 'snapshot':{'items':[]}}])
            return web.json_response({'owner':'A', 'items':[]})
        app = web.Application()
        app.router.add_route('*', '/{path:.*}', handle)
        async with TestServer(app) as server, ClientSession() as session:
            api = API(session, {'url':str(server.make_url('')), 'api_key':'satellite-key', 'satellite':'A'})
            assert (await api.snapshot())['id'] == 'A'
            assert (await api.command({'action':'list'}))['owner'] == 'A'
            assert len(calls) == 2 and all('/auth/' not in path for _,path,_ in calls)
            assert all(header == 'Bearer satellite-key' for _,_,header in calls)
            api = API(session, {'url':str(server.make_url('')), 'api_key':'revoked', 'satellite':'A'})
            with pytest.raises(HomeAssistantError):
                await api.snapshot()
            assert len(calls) == 3  # no password fallback or retry with a revoked key
    asyncio.run(run())


def test_config_flow_has_masked_key_and_rejects_ingress_url(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        flow = ScheduleFlow()
        flow.hass = hass
        flow.context = {'source':'user'}
        form = await flow.async_step_user()
        fields = {str(key):value for key,value in form['data_schema'].schema.items()}
        assert 'username' not in fields and 'password' not in fields
        assert fields['api_key'].config['type'] == 'password'
        with patch('custom_components.echomuse_schedule.config_flow.async_get_clientsession'), patch.object(API, 'snapshot', new=AsyncMock()) as snapshot:
            form = await flow.async_step_user({'url':'http://ha:8123/api/hassio_ingress/session/', 'api_key':'key', 'satellite':'A'})
            assert form['errors'] == {'base':'cannot_connect'}
            snapshot.assert_not_called()
        await hass.async_stop(force=True)
    asyncio.run(run())


def test_config_flow_accepts_key_and_preserves_satellite_identity(tmp_path):
    async def run():
        hass = HomeAssistant(str(tmp_path))
        flow = ScheduleFlow()
        flow.hass = hass
        flow.context = {'source':'user'}
        with patch('custom_components.echomuse_schedule.config_flow.async_get_clientsession'), \
             patch.object(API, 'snapshot', new=AsyncMock(return_value={'id':'A','mac':'02:00:00:00:00:01'})), \
             patch.object(flow, 'async_set_unique_id', new=AsyncMock()) as set_id, \
             patch.object(flow, '_abort_if_unique_id_configured'):
            result = await flow.async_step_user({'url':' http://ha:8768 ', 'api_key':' satellite-key ', 'satellite':'A', 'tts_engine':''})
            assert result['type'] == 'create_entry'
            assert result['data']['api_key'] == 'satellite-key'
            assert result['data']['url'] == 'http://ha:8768'
            assert 'username' not in result['data']
            set_id.assert_awaited_once_with('A')
        await hass.async_stop(force=True)
    asyncio.run(run())


def test_reconfigure_replaces_credentials_but_cannot_change_satellite(tmp_path):
    async def run():
        from types import SimpleNamespace
        hass = HomeAssistant(str(tmp_path))
        flow = ScheduleFlow()
        flow.hass = hass
        flow.context = {'source':'reconfigure'}
        entry = SimpleNamespace(unique_id='A',data={'url':'http://ha:8768','satellite':'A','username':'legacy','password':'legacy'})
        values = {'url':'http://ha:8768','satellite':'A','api_key':'new-key'}
        with patch.object(flow, '_get_reconfigure_entry', return_value=entry), \
             patch('custom_components.echomuse_schedule.config_flow.async_get_clientsession'), \
             patch.object(API, 'snapshot', new=AsyncMock(return_value={'id':'A','mac':'02:00:00:00:00:01'})), \
             patch.object(flow, 'async_update_reload_and_abort', return_value={'type':'abort'}) as update:
            await flow.async_step_reconfigure(values)
            assert update.call_args.kwargs['data'] == {**values,'mac':'02:00:00:00:00:01'}
            assert 'password' not in update.call_args.kwargs['data']
        with patch.object(flow, '_get_reconfigure_entry', return_value=entry), \
             patch('custom_components.echomuse_schedule.config_flow.async_get_clientsession'), \
             patch.object(API, 'snapshot', new=AsyncMock(return_value={'id':'B','mac':'02:00:00:00:00:02'})):
            result = await flow.async_step_reconfigure(values)
            assert result['reason'] == 'wrong_satellite'
        await hass.async_stop(force=True)
    asyncio.run(run())
