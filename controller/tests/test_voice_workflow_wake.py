"""Device wake/session routing and protocol-failure recovery regressions."""
import asyncio
import json
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

import em_db as db
import em_listen
import em_workflow as W
from test_voice_workflow import device, until, va_hello


@pytest.fixture
def fresh_db(tmp_path):
    db.init(str(tmp_path / 'wake.db'))
    db.ensure_device_token('satellite')
    db.set_device_config_sections('satellite',['wakeword'])
    cfg=db.get_device_config('satellite')
    db.set_device_config('satellite',{**cfg,'owwOnDevice':'on','owwModel':'chosen_wake.onnx'})
    yield
    db._conn.close()
    db._conn=None


@pytest.mark.parametrize('interrupt_by',['wake','voice'])
def test_private_wake_gates_real_data_and_preserves_chosen_detector(fresh_db,monkeypatch,interrupt_by):
    async def main():
        import em_controller as C
        d=device()
        d.capabilities=['oww_local_only']
        d.oww_on_device='on'
        d.listen_reported='local'
        d.data_ws=None
        d.data_ready.clear()
        monkeypatch.setattr(C,'_devices',{d.device_id:d})
        monkeypatch.setattr(C,'_link_auth_ok',AsyncMock(return_value=True))
        monkeypatch.setattr(C,'_not_from_control',lambda *a:None)
        monkeypatch.setattr(C.em_ble_proxy,'sync_slots',AsyncMock())
        monkeypatch.setattr(C,'_push_device_state',AsyncMock())
        heard,actions,outputs=[],[],[]
        peer=[]
        async def server(ws):
            peer.append(ws)
            await ws.send(json.dumps(va_hello()))
            async for raw in ws:
                if isinstance(raw,str):
                    actions.append(json.loads(raw)['type'])
                else:
                    heard.append(raw)
        async with serve(server,'127.0.0.1',0) as host, serve(C.handle_data,'127.0.0.1',0) as satellite_host:
            async with connect(f'ws://127.0.0.1:{satellite_host.sockets[0].getsockname()[1]}') as satellite:
                await satellite.send(json.dumps({'type':'identify','device_id':d.device_id}))
                await asyncio.wait_for(d.data_ready.wait(),2)
                socket=d.data_ws
                async def collect():
                    async for raw in satellite:
                        outputs.append(raw)
                reader=asyncio.create_task(collect())
                listener=None
                def frame(session,value):
                    return struct.pack('>BIH',em_listen.FRAME_TYPE,session,0)+bytes([value,0])*1280
                try:
                    cfg={'mode':W.PIPECAT,'interrupt_by':interrupt_by,'endpoint':f'ws://127.0.0.1:{host.sockets[0].getsockname()[1]}/api/assist/esphome'}
                    await d.workflow.apply(cfg,await W.handshake(cfg,d.device_id))
                    await until(lambda:d.workflow.status=='connected')
                    await peer[0].send(b'\x11\x00'*2400)  # unsolicited server speech
                    await satellite.send(frame(42,1))    # audio arrives BEFORE wake on control
                    ambient=bytes([C.MIC_FRAME_TYPE])+bytes(C.MIC_HEADER_LEN-1)+b'\x44\x00'*1280
                    await satellite.send(ambient)
                    await asyncio.sleep(.05)
                    assert heard==[] and actions==[]
                    assert not any(isinstance(raw,bytes) and raw[:1]==b'\x02' for raw in outputs)
                    ev=em_listen.parse_wake({'session':42,'score':.91,'ageMs':0},asyncio.get_running_loop().time())
                    d.listen_wakes.put_nowait(ev)
                    listener=asyncio.create_task(C.wake_word_listener(d))
                    await until(lambda:len(heard)==1)
                    assert actions==['wake']
                    assert heard==[b'\x01\x00'*1280]
                    controls=[json.loads(c.args[0]) for c in d.control_ws.send.call_args_list]
                    assert {'type':'listen_ack','session':42} in controls
                    configs=[c for c in controls if c['type']=='config']
                    assert all(c.get('owwOnDevice')=='on' for c in configs)
                    assert configs[0]['owwModel']=='chosen_wake.onnx'
                    await peer[0].send(b'\x12\x00'*2400)
                    await until(lambda:any(isinstance(raw,bytes) and raw[:1]==b'\x02' for raw in outputs))
                    await satellite.send(frame(42,2))
                    if interrupt_by=='voice':
                        # Voice interruption: the microphone stays open under the reply.
                        await until(lambda:len(heard)==2)
                    else:
                        # Wake-word interruption: nothing reaches the server while the
                        # reply plays, so the speaker's own echo cannot be heard as the user.
                        await asyncio.sleep(.1)
                        assert len(heard)==1
                    await satellite.send(frame(99,99))  # another session cannot leak
                    await satellite.send(ambient)      # nor can the ambient 0x01 stream
                    await peer[0].send(json.dumps({'type':'phase','phase':'thanks','terminal':True}))
                    await until(lambda:not d.workflow.awake)
                    await satellite.send(frame(42,3))
                    await asyncio.sleep(.05)
                    sent=[b'\x01\x00'*1280]+([b'\x02\x00'*1280] if interrupt_by=='voice' else [])
                    assert heard==sent
                    # The next normal device wake starts a fresh routed session.
                    await satellite.send(frame(43,4))
                    d.listen_wakes.put_nowait(em_listen.parse_wake({'session':43,'score':.95},asyncio.get_running_loop().time()))
                    await until(lambda:len(heard)==len(sent)+1)
                    assert heard[-1]==b'\x04\x00'*1280
                    assert d.data_ws is socket and C._devices[d.device_id] is d
                    assert not d.control_ws.close.called
                finally:
                    if listener:
                        listener.cancel()
                        await asyncio.gather(listener,return_exceptions=True)
                    await d.workflow.stop()
                    reader.cancel()
                    await asyncio.gather(reader,return_exceptions=True)
    asyncio.run(main())


def test_immediate_protocol_failures_back_off_without_idle_eos(monkeypatch):
    async def main():
        d=device()
        d.workflow.config={'mode':W.PIPECAT,'endpoint':'ws://unused'}
        delays=[]
        monkeypatch.setattr(W,'handshake',AsyncMock(side_effect=lambda *a:SimpleNamespace(close=AsyncMock())))
        async def fail(ws):
            raise W.ProtocolError('expected_protobuf_received_text')
        d.workflow.receive=fail
        async def pause(delay):
            delays.append(delay)
            if len(delays)==3:
                d.workflow.config={'mode':W.NATIVE}
            await asyncio.sleep(0)
        monkeypatch.setattr(W,'retry_delay',pause)
        await d.workflow.run()
        assert delays==[1,2,4]
        assert d.workflow.last_error=='expected_protobuf_received_text'
        d.data_ws.send.assert_not_called()
        d.control_ws.close.assert_not_called()
    asyncio.run(main())


@pytest.mark.parametrize('condition',['muted','wake_disabled','expired','closed'])
def test_invalid_device_wakes_never_open_audio(condition):
    async def main():
        d=device()
        d.workflow.config={'mode':W.PIPECAT}
        d.workflow.status='connected'
        if condition=='muted': d.muted=True
        if condition=='wake_disabled': d.wake_word_enabled=False
        if condition=='closed': d.listen_router.close(7)
        assert not await d.workflow.wake(7,age_ms=4000 if condition=='expired' else 0)
        d.workflow.feed(b'private',7)
        assert not d.workflow.awake and d.workflow.queue.empty()
        controls=[json.loads(c.args[0]) for c in d.control_ws.send.call_args_list]
        assert not any(c['type']=='listen_ack' for c in controls)
    asyncio.run(main())


def test_session_deadline_closes_uplink_and_stale_frames():
    async def main():
        d=device()
        d.workflow.config={'mode':W.PIPECAT}
        d.workflow.status='connected'
        assert await d.workflow.wake(7)
        d.workflow.feed(b'queued',7)
        d.workflow.session_deadline=0
        await d.workflow.poll()
        assert not d.workflow.awake and d.workflow.queue.empty()
        d.workflow.feed(b'late',7)
        assert d.workflow.queue.empty() and d.listen_router.is_closed(7)
    asyncio.run(main())


def test_button_session_restores_local_wake_mode(fresh_db):
    async def main():
        d=device()
        d.workflow.config={'mode':W.PIPECAT}
        d.workflow.status='connected'
        assert await d.workflow.wake(button=True)
        await d.workflow.end_session(reason='button_done')
        controls=[json.loads(c.args[0]) for c in d.control_ws.send.call_args_list]
        configs=[c for c in controls if c['type']=='config']
        assert configs[0]['owwOnDevice']=='shadow'  # classifier stays enabled
        assert configs[-1]['owwOnDevice']=='on'
        assert all(c['owwModel']=='chosen_wake.onnx' for c in configs)
    asyncio.run(main())


def test_first_speech_after_wake_keeps_its_partial_speaker_period():
    async def main():
        d=device()
        d.workflow.config={'mode':W.PIPECAT}
        d.workflow.status='connected'
        queue=asyncio.Queue()
        waiting=asyncio.Event()
        async def recv():
            waiting.set()
            return await queue.get()
        task=asyncio.create_task(d.workflow.receive(SimpleNamespace(recv=recv)))
        try:
            await waiting.wait()  # receiver was already awaiting data while idle
            assert await d.workflow.wake(7)
            audio=W.Frame()
            pcm=b'\x12\x00'*200
            audio.audio.audio=pcm
            audio.audio.sample_rate=48000
            audio.audio.num_channels=1
            await queue.put(audio.SerializeToString())
            await until(lambda:any(c.args[0].startswith(b'\x02') for c in d.data_ws.send.call_args_list))
            frames=[c.args[0] for c in d.data_ws.send.call_args_list]
            assert frames==[b'\x02'+pcm.ljust(4096,b'\0'),b'\x03']
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
    asyncio.run(main())


def test_button_opens_an_idle_session_interrupts_a_reply_and_stops_listening(fresh_db):
    async def main():
        d=device()
        d.workflow.config={'mode':W.PIPECAT}
        d.workflow.status='connected'
        try:
            await d.workflow.button()
            assert d.workflow.awake                       # idle: opens a session
            await d.workflow.button()
            assert not d.workflow.awake                   # only listening: stops it
            assert d.workflow.queue.empty() and not d.workflow.button_capture
            await d.workflow.button()
            assert d.workflow.awake
            d.workflow.output_queue=asyncio.Queue()       # a reply is playing
            await d.workflow.button()
            assert d.workflow.awake and d.workflow.output_queue is None   # cut short, still listening
            await d.workflow.button()
            assert not d.workflow.awake                   # now nothing is playing
        finally:
            await d.workflow.stop()
    asyncio.run(main())


def test_button_while_the_server_is_thinking_stops_the_session(fresh_db):
    async def main():
        d=device()
        d.workflow.config={'mode':W.PIPECAT}
        d.workflow.status='connected'
        try:
            assert await d.workflow.wake(button=True)
            d.workflow.accept_audio=False                 # utterance sent, waiting for the model
            await d.workflow.button()
            assert not d.workflow.awake
        finally:
            await d.workflow.stop()
    asyncio.run(main())


def _orphan_bridge(monkeypatch):
    monkeypatch.setattr(W,'ORPHAN_STOP_S',.05)
    monkeypatch.setattr(W,'ORPHAN_RECYCLE_S',.15)
    d=device()
    d.workflow.config={'mode':W.PIPECAT,'endpoint':'ws://pipecat/api/assist/esphome'}
    d.workflow.status='connected'
    events=asyncio.Queue()
    ws=SimpleNamespace(recv=events.get,send=AsyncMock(),echomuse_audio_format=(24000,1))
    return d,events,ws


async def _speak(events,seconds,gap=.02):
    end=asyncio.get_running_loop().time()+seconds
    while asyncio.get_running_loop().time()<end:
        events.put_nowait(b'\x01\x00'*480)
        await asyncio.sleep(gap)


def test_a_little_speech_after_a_session_closed_is_dropped_quietly(monkeypatch):
    async def main():
        d,events,ws=_orphan_bridge(monkeypatch)
        task=asyncio.create_task(d.workflow.receive(ws))
        await _speak(events,.03)
        await asyncio.sleep(.1)
        assert not task.done()
        ws.send.assert_not_awaited()
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
    asyncio.run(main())


def test_speech_that_goes_on_with_no_session_is_stopped_then_the_connection_recycled(monkeypatch):
    async def main():
        d,events,ws=_orphan_bridge(monkeypatch)
        task=asyncio.create_task(d.workflow.receive(ws))
        feeder=asyncio.create_task(_speak(events,.5))
        with pytest.raises(W.ProtocolError,match='unsolicited_pipecat_speech'):
            await asyncio.wait_for(task,2)
        feeder.cancel()
        await asyncio.gather(feeder,return_exceptions=True)
        ws.send.assert_awaited_once_with('{"type":"stop"}')
    asyncio.run(main())


def test_a_wake_clears_the_unsolicited_speech_count(fresh_db,monkeypatch):
    async def main():
        d,events,ws=_orphan_bridge(monkeypatch)
        d.workflow.orphan_since=asyncio.get_running_loop().time()-1
        d.workflow.orphan_stopped=True
        assert await d.workflow.wake(button=True)
        assert d.workflow.orphan_since is None and not d.workflow.orphan_stopped
        await d.workflow.stop()
    asyncio.run(main())


def test_a_refused_wake_says_why(caplog):
    async def main():
        d=device()
        d.workflow.config={'mode':W.PIPECAT}
        d.workflow.status='reconnecting'
        with caplog.at_level('WARNING',logger='echomuse.workflow'):
            assert not await d.workflow.wake(button=True)
    asyncio.run(main())
    assert any('Pipecat wake refused' in r.message and 'status=reconnecting' in r.message for r in caplog.records)
