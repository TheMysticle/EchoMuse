"""Exercise the real HTTP stack: scoped keys cross ingress, sessions do not."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
import em_api as api
import em_db as db
import em_schedule_auth as keys
from em_schedule import Bridge


@pytest.fixture
def database(tmp_path):
    db.init(str(tmp_path / 'controller.db'))
    for device_id in ('A', 'B'):
        db.register_new_device(device_id, '192.0.2.10', 'test')
        db.approve_device(device_id, device_id)
    user = db.create_user('admin', 'unused', 'admin')
    db.create_session('dashboard-session', user)
    reader = db.create_user('reader', 'unused', 'readonly')
    db.create_session('readonly-session', reader)
    yield str(tmp_path / 'controller.db')
    db._conn.close()
    db._conn = None


def test_persistent_hash_only_rotation_revocation_and_device_deletion(database):
    token = keys.issue('A')
    assert keys._resolve(token) == 'A'
    db._conn.close()
    db._conn = None
    db.init(database)
    assert keys._resolve(token) == 'A'
    assert token not in json.dumps(db.get_all_config())
    replacement = keys.issue('A')
    assert keys._resolve(token) is None
    assert keys._resolve(replacement) == 'A'
    keys.revoke('A')
    assert keys._resolve(replacement) is None
    token = keys.issue('B')
    db.delete_device('B')
    assert keys._resolve(token) is None
    db.register_new_device('B', '192.0.2.10', 'test')
    db.approve_device('B', 'B')
    assert keys._resolve(token) is None


@pytest.mark.parametrize('value', ['', 'emsched.', 'emsched.bad.bad', 'emsched.@@@.bad', 'emsched.'+'x'*1100, 'Bearer dashboard-session'])
def test_invalid_keys_fail_closed(database, value):
    assert keys._resolve(value) is None


def test_real_ingress_and_standalone_routes(database):
    async def run():
        live = {}
        for device_id in ('A', 'B'):
            bridge = Bridge(device_id)
            bridge.update({'owner':device_id, 'revision':1, 'items':[]})
            async def send(message, bridge=bridge):
                bridge.receive({'id':message['id'], 'snapshot':{**bridge.snapshot, 'revision':2}})
            live[device_id] = SimpleNamespace(capabilities=['local_schedule'], schedule=bridge, send_control=send)
        with patch.object(api, '_devices', live), patch.object(api, 'INGRESS_ONLY', True), patch.object(api, 'INGRESS_GATEWAY_IP', '127.0.0.1'):
            async with TestClient(TestServer(await api.create_app())) as client:
                # The genuine ingress admin creates a scoped key, never a password.
                r = await client.post('/api/devices/A/schedule-connection', headers={'Authorization':'Bearer dashboard-session'}, json={})
                assert r.status == 200, await r.text()
                assert r.headers['Cache-Control'] == 'no-store'
                key = (await r.json())['api_key']
                r = await client.post('/api/devices/A/schedule-connection', headers={'Authorization':'Bearer readonly-session'}, json={})
                assert r.status == 403
                headers = {'Authorization':'Bearer '+key}
                with patch.object(api, 'INGRESS_GATEWAY_IP', '172.30.32.2'):
                    r = await client.get('/api/schedules', headers=headers)
                    assert r.status == 200, await r.text()
                    assert [row['id'] for row in await r.json()] == ['A']
                    r = await client.post('/api/devices/A/schedules', headers=headers, json={'action':'list'})
                    assert r.status == 200, await r.text()
                    assert (await r.json())['owner'] == 'A'
                    # New sound settings must never be silently sent to old firmware.
                    command = {'action':'settings', 'settings':{'sounds':{'alarm':'a'*64}}}
                    with patch.object(live['A'].schedule, 'request', wraps=live['A'].schedule.request) as transport:
                        r = await client.post('/api/devices/A/schedules', headers=headers, json=command)
                        assert r.status == 409, await r.text()
                        assert 'Update satellite firmware' in (await r.json())['error']
                        transport.assert_not_called()
                        live['A'].capabilities.append('local_schedule_sounds')
                        r = await client.post('/api/devices/A/schedules', headers=headers, json=command)
                        assert r.status == 200, await r.text()
                        assert transport.call_args.args[1] == command
                    # Test asset access with conversion mocked, not the permission checks.
                    with patch('em_schedule.convert_audio', return_value=('asset-hash', b'pcm')):
                        r = await client.post('/api/devices/A/schedule-assets', headers=headers, data=b'audio')
                        assert r.status == 200, await r.text()
                    for method, path in (
                        ('POST','/api/devices/B/schedules'), ('POST','/api/devices/B/schedule-assets'),
                        ('GET','/api/devices'), ('GET','/api/devices/A/config'),
                        ('POST','/api/devices/A/schedule-connection'), ('DELETE','/api/devices/A/schedule-connection'),
                        ('POST','/api/auth/login'), ('GET','/api/devices/A/shell'),
                        ('GET','/api/schedules/'), ('HEAD','/api/schedules'),
                    ):
                        r = await client.request(method, path, headers=headers)
                        assert r.status == 403, (method, path, r.status)
                    # Even real admin sessions and spoofed ingress headers cannot bypass the gateway.
                    for h in ({}, {'Authorization':'Bearer dashboard-session'}, {'X-Remote-User-Id':'admin'}, {'Authorization':'Bearer '+key+'bad'}):
                        r = await client.get('/api/schedules', headers=h)
                        assert r.status == 403
                    # Keys also work on standalone controllers, but keep their scope.
                    with patch.object(api, 'INGRESS_ONLY', False):
                        r = await client.get('/api/schedules', headers=headers)
                        assert r.status == 200
                        r = await client.post('/api/devices/B/schedules', headers=headers, json={'action':'list'})
                        assert r.status == 401
                        r = await client.get('/api/devices', headers=headers)
                        assert r.status == 401
                    keys.revoke('A')
                    r = await client.get('/api/schedules', headers=headers)
                    assert r.status == 403
    asyncio.run(run())
