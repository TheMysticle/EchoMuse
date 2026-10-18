"""Exercise routing against real WebSockets without a model key or satellite."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
from websockets.asyncio.server import serve

import em_db as db
import em_workflow as W


@pytest.fixture
def fresh_db(tmp_path):
    db.init(str(tmp_path / "workflow.db"))
    db.ensure_device_token("satellite")
    yield
    db._conn.close()
    db._conn = None


def device():
    import _oww_stub
    _oww_stub.install()
    try:
        import em_controller as C
    finally:
        _oww_stub.uninstall()
    d = C.Device("satellite", "127.0.0.1", [], SimpleNamespace(send=AsyncMock(), close=AsyncMock()))
    d.data_ws = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
    d.data_ready.set()
    d.capabilities = ['output_chain']
    async def playback_report(raw):
        if raw == b'\x03':
            d.signal_playback_done()
    d.data_ws.send.side_effect = playback_report
    d._set_speaking = AsyncMock()
    return d


async def until(predicate):
    async with asyncio.timeout(4):
        while not predicate():
            await asyncio.sleep(.01)


def test_persistence_default_secrets_and_delete(fresh_db):
    assert db.get_voice_workflow("satellite") == {"mode": W.NATIVE}
    cfg = W.validate({"mode": W.PIPECAT, "endpoint": "ws://localhost:8765", "api_key": "private"}, {})
    db.set_voice_workflow("satellite", cfg)
    assert db.get_voice_workflow("satellite") == cfg
    assert "private" not in json.dumps(W.public(cfg))
    assert "api_key" not in W.public(cfg)
    assert db.get_device_config("satellite").get("endpoint") is None
    assert not any("private" in str(v) for v in db.get_all_config().values())
    db.delete_device("satellite")
    assert db.get_voice_workflow("satellite") == {"mode": W.NATIVE}


@pytest.mark.parametrize("body", [None, [], {"mode":"other"},
    {"mode":W.PIPECAT,"endpoint":"https://host"},
    {"mode":W.PIPECAT,"endpoint":"ws://user:password@host"},
    {"mode":W.PIPECAT,"endpoint":"ws://host:invalid"},
    {"mode":W.PIPECAT,"endpoint":"ws://host", "api_key":"x\r\ninjected"},
    {"mode":W.NATIVE,"unexpected":True}])
def test_validation_rejects_bad_configuration(body):
    with pytest.raises((ValueError, TypeError)):
        W.validate(body, {})


def test_protobuf_matches_pipecat_wire_schema():
    f = W.Frame()
    f.audio.audio = b"\x01\x02"
    f.audio.sample_rate = 16000
    f.audio.num_channels = 1
    # Frame.audio=2; AudioRawFrame audio=3, sample_rate=4, num_channels=5.
    assert f.SerializeToString() == bytes.fromhex("12091a02010220807d2801")


@pytest.mark.parametrize("rate", [8000,16000,22050,24000,44100,48000])
def test_pcm_bridge_rate_and_stereo(rate):
    pcm = np.full((rate // 10, 2), 1000, dtype="<i2").tobytes()
    out = W.speaker_pcm(pcm, rate, 2)
    assert len(out) == 4800 * 2
    assert abs(np.frombuffer(out, dtype="<i2")[100:-100].mean() - 1000) < 2


def test_pcm_rejects_malformed_audio():
    for args in ((b"a", 16000, 1), (b"aa", 48000, 3), (b"aa", 1234, 1)):
        with pytest.raises(ValueError):
            W.speaker_pcm(*args)


def test_full_duplex_switch_preserves_control_and_data(fresh_db, monkeypatch):
    import em_esphome as E
    cancel = []
    monkeypatch.setattr(E, "cancel_voice_turn", lambda *a, **kw: cancel.append((a,kw)))
    async def main():
        d = device()
        control, data = d.control_ws, d.data_ws
        received = []
        headers = []
        ready = asyncio.Event()
        async def server(ws):
            headers.append(ws.request.headers)
            ready.set()
            async for raw in ws:
                received.append(W.Frame.FromString(raw))
                audio = W.Frame()
                audio.audio.audio = np.full(4800, 500, dtype="<i2").tobytes()
                audio.audio.sample_rate = 24000
                audio.audio.num_channels = 1
                await ws.send(audio.SerializeToString())
                # Wait for MORE microphone audio while returning speech.
                second = await ws.recv()
                received.append(W.Frame.FromString(second))
                interruption = W.Frame()
                interruption.interruption.SetInParent()
                await ws.send(interruption.SerializeToString())
                await ws.wait_closed()
                break
        async with serve(server, "127.0.0.1", 0) as host:
            port = host.sockets[0].getsockname()[1]
            cfg = {"mode":W.PIPECAT, "endpoint":f"ws://127.0.0.1:{port}",
                   "api_key":"server-secret", "google_api_key":"google-secret",
                   "area":"kitchen", "target_device":"lamp"}
            ws = await W.handshake(cfg, d.device_id)
            await ready.wait()
            async with d.workflow.lock:
                await d.workflow.apply(cfg, ws)
            await until(lambda: d.workflow.status == "connected")
            await d.workflow.wake()
            d.workflow.feed(b"\x01\x00" * 1280)
            await until(lambda: any(c.args[0].startswith(b"\x02") for c in data.send.call_args_list))
            d.workflow.feed(b"\x02\x00" * 1280)
            await until(lambda: len(received) == 2)
            assert received[0].audio.sample_rate == 16000
            assert received[0].audio.audio == b"\x01\x00" * 1280
            assert headers[0]["Authorization"] == "Bearer server-secret"
            assert headers[0]["X-EchoMuse-Google-Api-Key"] == "google-secret"
            assert headers[0]["X-EchoMuse-Area"] == "kitchen"
            await d.send_control({"type":"ping", "id":99})
            async with d.workflow.lock:
                await d.workflow.apply({"mode":W.NATIVE})
            assert d.workflow.status == "native"
            assert d.workflow.task is None
            assert d.control_ws is control and d.data_ws is data
            control.close.assert_not_called()
            data.close.assert_not_called()
            controls = [json.loads(c.args[0]) for c in control.send.call_args_list]
            assert {"type":"ping", "id":99} in controls
            configs = [c for c in controls if c["type"] == "config"]
            assert configs[0]["owwOnDevice"] == db.get_effective_device_config("satellite")["owwOnDevice"]
            assert configs[-1]["owwOnDevice"] == db.get_effective_device_config("satellite")["owwOnDevice"]
            assert "server-secret" not in json.dumps(controls)
            assert len(cancel) == 2
    asyncio.run(main())


def test_queue_bounded_and_muting_drops_microphone():
    d = device()
    d.workflow.config = {"mode": W.PIPECAT}
    d.workflow.status = "connected"
    d.workflow.awake = True
    for n in range(100):
        d.workflow.feed(bytes([n]))
    assert d.workflow.queue.qsize() == 64
    assert d.workflow.queue.get_nowait()[1] == bytes([36])
    d.muted = True
    d.workflow.feed(b"private")
    assert d.workflow.queue.qsize() == 63


def test_api_refuses_failed_handshake_and_redacts_keys(fresh_db, monkeypatch):
    import em_api as A
    async def main():
        async def auth(request):
            return {"role":"admin"}
        monkeypatch.setattr(A.auth, "resolve_session", auth)
        monkeypatch.setattr(A, "_devices", {})
        req = SimpleNamespace(match_info={"id":"satellite"}, json=AsyncMock(return_value={
            "mode":W.PIPECAT, "endpoint":"ws://127.0.0.1:1", "api_key":"never-log-this"}))
        # Direct handler drives request parsing; permission behavior covered below.
        result = await A._post_voice_workflow.__wrapped__(req)
        assert result.status == 400
        assert db.get_voice_workflow("satellite") == {"mode": W.NATIVE}
        assert "never-log-this" not in result.text
        cfg = {"mode":W.NATIVE, "api_key":"server-secret", "google_api_key":"model-secret"}
        db.set_voice_workflow("satellite", cfg)
        result = await A._get_voice_workflow.__wrapped__(req)
        assert "server-secret" not in result.text and "model-secret" not in result.text
        assert json.loads(result.text)["has_api_key"]
    asyncio.run(main())


def test_api_requires_admin_for_changes(monkeypatch):
    import em_api as A
    async def main():
        monkeypatch.setattr(A.auth, "resolve_session", AsyncMock(return_value={"role":"readonly"}))
        assert (await A._post_voice_workflow({})).status == 403
        monkeypatch.setattr(A.auth, "resolve_session", AsyncMock(return_value=None))
        assert (await A._post_voice_workflow({})).status == 401
    asyncio.run(main())


def test_pipecat_blocks_native_turn_entry(fresh_db):
    import em_esphome as E
    async def main():
        d = device()
        d.workflow.config = {"mode":W.PIPECAT}
        native = E.EchoMuseSatellite.__new__(E.EchoMuseSatellite)
        # Refusal happens before any HA transport or model access.
        await native.run_esphome_voice_turn(d, AsyncMock(), AsyncMock())
        import em_controller as C
        await C._run_voice_locked(d)
        assert not d.voice_lock.locked()
    asyncio.run(main())


def test_api_switch_routes_real_satellite_frames_without_reconnecting(fresh_db, monkeypatch):
    from websockets.asyncio.client import connect
    import em_controller as C
    import em_api as A
    import em_esphome as E
    async def main():
        d = device()
        d.data_ws = None
        d.data_ready.clear()
        registry = {d.device_id:d}
        monkeypatch.setattr(C, "_devices", registry)
        monkeypatch.setattr(A, "_devices", registry)
        monkeypatch.setattr(C, "_link_auth_ok", AsyncMock(return_value=True))
        monkeypatch.setattr(C, "_not_from_control", lambda *a: None)
        monkeypatch.setattr(C.em_ble_proxy, "sync_slots", AsyncMock())
        monkeypatch.setattr(E, "cancel_voice_turn", lambda *a, **kw: None)
        heard = []
        async def pipecat(ws):
            async for raw in ws:
                heard.append(W.Frame.FromString(raw).audio.audio)
        async with serve(pipecat, "127.0.0.1", 0) as pc, serve(C.handle_data, "127.0.0.1", 0) as data:
            async with connect(f"ws://127.0.0.1:{data.sockets[0].getsockname()[1]}") as satellite:
                await satellite.send(json.dumps({"type":"identify", "device_id":d.device_id}))
                await asyncio.wait_for(d.data_ready.wait(), 2)
                channel = d.data_ws
                body = {"mode":W.PIPECAT, "endpoint":f"ws://127.0.0.1:{pc.sockets[0].getsockname()[1]}"}
                req = SimpleNamespace(match_info={"id":d.device_id}, json=AsyncMock(return_value=body))
                assert (await A._post_voice_workflow.__wrapped__(req)).status == 200
                await until(lambda: d.workflow.status == "connected")
                await d.workflow.wake()
                header = bytes([C.MIC_FRAME_TYPE]) + bytes(C.MIC_HEADER_LEN-1)
                pcm = b"\x20\x00" * 1280
                await satellite.send(header + pcm)
                await until(lambda: heard)
                assert heard == [pcm]
                # Selecting native changes queues, not the data socket.
                req.json = AsyncMock(return_value={"mode":W.NATIVE})
                assert (await A._post_voice_workflow.__wrapped__(req)).status == 200
                d.oww_paused.set()
                await satellite.send(header + b"\x30\x00" * 1280)
                await until(lambda: not d.voice_queue.empty())
                assert bytes(d.voice_queue.get_nowait()) == b"\x30\x00" * 1280
                assert heard == [pcm]
                assert d.data_ws is channel and registry[d.device_id] is d
                assert not d.control_ws.close.called
                await d.send_control({"type":"ping"})
                assert json.loads(d.control_ws.send.call_args.args[0]) == {"type":"ping"}
    asyncio.run(main())


def test_short_speech_flushes_even_while_metadata_keeps_arriving():
    async def main():
        d = device()
        d.workflow.awake = True
        queue = asyncio.Queue()
        ws = SimpleNamespace(recv=queue.get)
        task = asyncio.create_task(d.workflow.receive(ws))
        try:
            audio = W.Frame()
            pcm = b"\x11\x00" * 200
            audio.audio.audio = pcm
            audio.audio.sample_rate = 48000
            audio.audio.num_channels = 1
            await queue.put(audio.SerializeToString())
            metadata = W.Frame()
            metadata.message.data = '{"transcript":"hello"}'
            for _ in range(8):
                await queue.put(metadata.SerializeToString())
                await asyncio.sleep(.05)
            frames = [c.args[0] for c in d.data_ws.send.call_args_list]
            assert frames == [b"\x02" + pcm.ljust(4096, b"\0"), b"\x03"]
            assert d._set_speaking.call_args_list[-1].args == (False,)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(main())


def test_pipecat_reconnect_does_not_replay_stale_audio(fresh_db):
    async def main():
        d = device()
        sessions, heard = [], []
        async def server(ws):
            sessions.append(ws)
            async for raw in ws:
                if len(sessions) == 1:
                    await ws.close()
                    return
                heard.append(W.Frame.FromString(raw).audio.audio)
        async with serve(server, "127.0.0.1", 0) as host:
            cfg = {"mode":W.PIPECAT, "endpoint":f"ws://127.0.0.1:{host.sockets[0].getsockname()[1]}"}
            try:
                await d.workflow.apply(cfg, await W.handshake(cfg, d.device_id))
                await until(lambda: d.workflow.status == "connected")
                await d.workflow.wake()
                d.workflow.feed(b"\x01\x00")
                await until(lambda: d.workflow.status == "reconnecting")
                d.workflow.feed(b"\x02\x00")
                await until(lambda: len(sessions) == 2 and d.workflow.status == "connected")
                await d.workflow.wake()
                d.workflow.feed(b"\x03\x00")
                await until(lambda: heard)
                assert heard == [b"\x03\x00"]
                assert d.workflow.active
                d.control_ws.close.assert_not_called()
                d.data_ws.close.assert_not_called()
            finally:
                await d.workflow.stop()
    asyncio.run(main())


def va_hello(**audio_changes):
    return {'type':'hello','protocol':'va-pipecat','version':1,
            'audio':{'format':'pcm_s16le','input_sample_rate':16000,
                     'output_sample_rate':24000,'channels':1,**audio_changes}}


def test_va_handshake_sends_stored_satellite_identity(fresh_db):
    async def main():
        expected = db.get_esphome_mac('satellite')
        async def server(ws):
            assert ws.request.headers['X-EchoMuse-Satellite-Mac'] == expected
            # This remains a memory key; it is not passed as an HA registry ID.
            assert ws.request.headers['X-EchoMuse-Target-Device'] == 'session-memory-key'
            await ws.send(json.dumps(va_hello()))
            await ws.wait_closed()
        async with serve(server, '127.0.0.1', 0) as host:
            cfg = {'mode': W.PIPECAT,
                   'endpoint': f'ws://127.0.0.1:{host.sockets[0].getsockname()[1]}/api/assist/esphome',
                   'target_device': 'session-memory-key'}
            ws = await W.handshake(cfg, 'satellite')
            await ws.close()
    asyncio.run(main())


@pytest.mark.parametrize('composed', [True, False])
def test_device_endpoint_moves_only_composed_pipeline_to_thinking(composed):
    async def main():
        events = []
        async def phase(value): events.append(('phase', value))
        async def send(value): events.append(('wire', value))
        d = SimpleNamespace(device_id='satellite', muted=False, private_listening=True,
                            workflow_phase=phase)
        bridge = W.Bridge(d)
        bridge.config = {'mode': W.PIPECAT, 'endpoint': 'ws://unused/api/assist/esphome'}
        bridge.awake = True
        bridge.status = 'connected'
        bridge.session = 1
        bridge.ws = SimpleNamespace(echomuse_device_endpoint=composed)
        bridge.feed(b'\x01\x00'*1280, 1)
        assert bridge.device_endpoint()
        sender = asyncio.create_task(bridge.send(SimpleNamespace(send=send)))
        try:
            await until(lambda: bridge.queue.empty())
            phases = [value for kind, value in events if kind == 'phase']
            assert phases == (['thinking'] if composed else [])
            wire = [value for kind, value in events if kind == 'wire']
            assert wire[1] == b'\x01\x00'*1280  # wake precedes the captured speech
            assert wire[2:] == [bytes(W.FRAME_BYTES)] * round(W.ENDPOINT_SILENCE_S*1000/80)
            if composed:
                assert events.index(('phase', 'thinking')) < events.index(('wire', bytes(W.FRAME_BYTES)))
                count = len(events)
                await bridge.on_event({'type': 'phase', 'phase': 'listening'})
                assert len(events) == count  # a late VAD event cannot relight listening
            bridge.feed(b'\x01\x00'*1280, 1)
            assert bridge.queue.empty()  # the microphone remains gated
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
    asyncio.run(main())


def test_va_pipecat_raw_pcm_and_semantic_events(fresh_db):
    async def main():
        from urllib.parse import urlsplit,parse_qs
        d=device()
        heard=[]
        actions=[]
        phases=[]
        d.workflow_phase=AsyncMock(side_effect=lambda phase:phases.append(phase))
        ready=asyncio.Event()
        async def server(ws):
            assert ws.request.headers['Authorization']=='Bearer satellite-secret'
            assert 'X-EchoMuse-Google-Api-Key' not in ws.request.headers
            assert ws.request.headers['X-EchoMuse-Scheduler-Url'].endswith('/voice-workflow/mcp')
            assert ws.request.headers['X-EchoMuse-Scheduler-Token']=='emvoice.test.scoped'
            assert ws.request.headers['X-EchoMuse-Local-Schedules']=='false'
            assert ws.request.headers['X-EchoMuse-Interrupt-By']=='wake'
            assert ws.request.headers['X-EchoMuse-Satellite-Mac']==db.get_esphome_mac('satellite')
            assert parse_qs(urlsplit(ws.request.path).query)['client_id']==['ha-device-id']
            await ws.send(json.dumps({**va_hello(),'echomuse':{'stop':True,'scheduler':False}}))
            async for raw in ws:
                if isinstance(raw,str):
                    actions.append(json.loads(raw)['type'])
                    continue
                heard.append(raw)
                await ws.send(json.dumps({'type':'phase','phase':'speaking'}))
                await ws.send(b'\x12\x00'*2400)
                await until(lambda:any(c.args[0].startswith(b'\x02') for c in d.data_ws.send.call_args_list))
                await ws.send(json.dumps({'type':'interrupt'}))
                await ws.send(json.dumps({'type':'phase','phase':'listening'}))
                ready.set()
        async with serve(server,'127.0.0.1',0) as host:
            cfg={'mode':W.PIPECAT,'endpoint':f'ws://127.0.0.1:{host.sockets[0].getsockname()[1]}/api/assist/esphome',
                 'api_key':'satellite-secret','google_api_key':'provider-key','target_device':'ha-device-id',
                 '_scheduler_url':'http://controller:8768/api/devices/satellite/voice-workflow/mcp',
                 '_scheduler_token':'emvoice.test.scoped','local_schedules':False}
            try:
                ws=await W.handshake(cfg,d.device_id)
                assert ws.echomuse_audio_format==(24000,1)
                await d.workflow.apply(cfg,ws)
                await until(lambda:d.workflow.status=='connected')
                await d.workflow.wake()
                d.workflow.feed(b'\x22\x00'*1280)
                await asyncio.wait_for(ready.wait(),2)
                await until(lambda:any(c.args[0].startswith(b'\x02') for c in d.data_ws.send.call_args_list))
                assert heard==[b'\x22\x00'*1280]
                assert actions==['wake']
                assert any(c.args[0].startswith(b'\x02') for c in d.data_ws.send.call_args_list)
                await d.workflow.interrupt()
                await until(lambda:actions==['wake','interrupt'])
                assert not d.control_ws.close.called and not d.data_ws.close.called
                assert W.public(cfg)['transport']=='va_pipecat'
            finally:
                await d.workflow.stop()
    asyncio.run(main())


@pytest.mark.parametrize('hello',[{},va_hello(input_sample_rate=48000),va_hello(format='opus'),
    va_hello(output_sample_rate=1234),va_hello(channels=2),{**va_hello(),'version':2}])
def test_va_pipecat_rejects_incompatible_hello(hello, fresh_db):
    async def main():
        async def server(ws):
            await ws.send(json.dumps(hello))
            await ws.wait_closed()
        async with serve(server,'127.0.0.1',0) as host:
            with pytest.raises(ValueError):
                await W.handshake({'endpoint':f'ws://127.0.0.1:{host.sockets[0].getsockname()[1]}/api/assist/esphome'},'test')
    asyncio.run(main())


def test_raw_transport_can_be_selected_for_a_reverse_proxy():
    cfg=W.validate({'mode':W.PIPECAT,'endpoint':'wss://example.test/voice','transport':'va_pipecat'}, {})
    assert W.transport(cfg)=='va_pipecat'
    with pytest.raises(ValueError):
        W.validate({**cfg,'transport':'unknown'}, {})


def test_google_setup_preserves_other_integrations():
    from aiohttp import web
    async def main():
        writes=[]
        async def get(request):
            assert request.headers['Authorization']=='Bearer satellite-secret'
            return web.json_response({'integrations':[
                {'id':'gemini','kind':'gemini','enabled':False,'api_key':'<redacted>'},
                {'id':'ha','kind':'home_assistant_mcp','token':'<redacted>','base_url':'http://ha/api/mcp'}]})
        async def put(request):
            writes.append(await request.json())
            return web.json_response({'ok':True})
        app=web.Application()
        app.router.add_get('/api/assist/config',get)
        app.router.add_put('/api/assist/config',put)
        runner=web.AppRunner(app)
        await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0)
        await site.start()
        try:
            port=site._server.sockets[0].getsockname()[1]
            await W.configure_google({'endpoint':f'ws://127.0.0.1:{port}/api/assist/esphome',
                                      'api_key':'satellite-secret','google_api_key':'new-google-key'})
            assert writes[0]['integrations'][0]['api_key']=='new-google-key'
            assert writes[0]['integrations'][0]['enabled'] is True
            assert writes[0]['integrations'][1]=={'id':'ha','kind':'home_assistant_mcp','token':'<redacted>','base_url':'http://ha/api/mcp'}
        finally:
            await runner.cleanup()
    asyncio.run(main())
