"""The Echo's end-of-speech marker reaches a Pipecat server as silence.

The server ends a turn by hearing quiet; it has no message for "the user is
done". So the Echo's endpointer (which cures a TV the server hears as speech)
is passed on as digital silence, after the speech already queued, and the
uplink is then closed until the next reply has been heard.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

import em_player
import em_state
import em_workflow as W
from test_voice_workflow import device, until, va_hello
import test_voice_workflow as workflow_fixtures

fresh_db = workflow_fixtures.fresh_db
SPEECH = b'\x07\x00' * 1280


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    device()
    import em_controller as C
    monkeypatch.setattr(C, '_push_device_state', AsyncMock())
    monkeypatch.setattr(em_player, '_sessions', {})


def bridge():
    d = device()
    d.capabilities = ['output_chain', 'oww_local_only']
    d.oww_on_device = 'on'
    d.listen_reported = 'local'
    d.workflow.config = {'mode': W.PIPECAT, 'endpoint': 'ws://unused/api/assist/esphome'}
    d.workflow.status = 'connected'
    d.data_ws.send.side_effect = None
    return d


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait()[1])
    return out


def test_speech_then_silence_then_a_closed_uplink(fresh_db):
    async def main():
        d = bridge()
        try:
            await d.workflow.wake(1)
            d.workflow.feed(SPEECH, 1)
            assert d.workflow.device_endpoint()
            frames = drain(d.workflow.queue)
            assert frames[0] == SPEECH                                  # what was said, first
            silence = frames[1:]
            assert len(silence) == round(W.ENDPOINT_SILENCE_S * 1000 / 80)
            assert all(f == bytes(W.FRAME_BYTES) for f in silence)
            d.workflow.feed(SPEECH, 1)                                  # a TV after the talker stopped
            assert d.workflow.queue.empty()
            assert not d.workflow.device_endpoint()                     # once per utterance
        finally:
            await d.workflow.stop()
    asyncio.run(main())


def test_a_new_session_and_a_heard_reply_reopen_the_uplink(fresh_db):
    async def main():
        d = bridge()
        try:
            await d.workflow.wake(1)
            d.workflow.device_endpoint()
            drain(d.workflow.queue)
            assert await d.workflow.wake(2)                             # wake word again
            d.workflow.feed(SPEECH, 2)
            assert not d.workflow.queue.empty()
            drain(d.workflow.queue)
            d.workflow.device_endpoint()
            await d.workflow.interrupt()                                # button / interruption
            d.workflow.feed(SPEECH, 2)
            assert not d.workflow.queue.empty()
        finally:
            await d.workflow.stop()
    asyncio.run(main())


def test_clarification_after_an_endpointed_question_hears_the_answer(fresh_db):
    async def main():
        d = bridge()
        events = asyncio.Queue()
        receiver = asyncio.create_task(d.workflow.receive(
            SimpleNamespace(recv=events.get, echomuse_audio_format=(24000, 1))))
        try:
            await d.workflow.wake(1)
            d.workflow.device_endpoint()
            drain(d.workflow.queue)
            await events.put(b'\x10\x00' * 2400)
            await events.put(json.dumps({'type': 'phase', 'phase': 'listening', 'follow_up': True,
                                         'continuation_question': True}))
            await until(lambda: d.workflow.draining)
            await until(lambda: any(c.args[0] == b'\x03' for c in d.data_ws.send.call_args_list))
            d.signal_playback_done()
            await until(lambda: not d.workflow.playback_tasks)
            d.workflow.feed(SPEECH, 1)
            assert not d.workflow.queue.empty()
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            await d.workflow.stop()
    asyncio.run(main())


@pytest.mark.parametrize('setup', ['asleep', 'thinking', 'muted'])
def test_a_marker_that_means_nothing_is_ignored(fresh_db, setup):
    async def main():
        d = bridge()
        try:
            if setup != 'asleep':
                await d.workflow.wake(1)
            if setup == 'thinking':
                d.workflow.accept_audio = False
            if setup == 'muted':
                d.muted = True
            assert not d.workflow.device_endpoint()
            assert d.workflow.queue.empty() and not d.workflow.endpointed
        finally:
            await d.workflow.stop()
    asyncio.run(main())


def test_the_marker_on_the_real_data_plane_reaches_the_server_as_silence(fresh_db, monkeypatch):
    async def main():
        import em_controller as C
        import em_listen
        import struct
        d = bridge()
        d.data_ws = None
        d.data_ready.clear()
        monkeypatch.setattr(em_state, 'devices', {d.device_id: d})
        monkeypatch.setattr(C, '_link_auth_ok', AsyncMock(return_value=True))
        monkeypatch.setattr(C, '_not_from_control', lambda *a: None)
        monkeypatch.setattr(C.em_ble_proxy, 'sync_slots', AsyncMock())
        heard, peer = [], []

        async def server(ws):
            peer.append(ws)
            await ws.send(json.dumps(va_hello()))
            async for raw in ws:
                if isinstance(raw, bytes):
                    heard.append(raw)

        async with serve(server, '127.0.0.1', 0) as host, serve(C.handle_data, '127.0.0.1', 0) as sat_host:
            async with connect(f'ws://127.0.0.1:{sat_host.sockets[0].getsockname()[1]}') as satellite:
                await satellite.send(json.dumps({'type': 'identify', 'device_id': d.device_id}))
                await asyncio.wait_for(d.data_ready.wait(), 2)
                cfg = {'mode': W.PIPECAT, 'endpoint': f'ws://127.0.0.1:{host.sockets[0].getsockname()[1]}/api/assist/esphome'}
                await d.workflow.apply(cfg, await W.handshake(cfg, d.device_id))
                await until(lambda: d.workflow.status == 'connected')
                await d.workflow.wake(42)
                await satellite.send(struct.pack('>BIH', em_listen.FRAME_TYPE, 42, 0) + SPEECH)
                await until(lambda: len(heard) == 1)
                marker = bytes([C.MIC_FRAME_TYPE]) + bytes(C.MIC_HEADER_LEN - 1) + bytes([C.VAD_END_TYPE])
                await satellite.send(marker)
                await until(lambda: len(heard) == 1 + round(W.ENDPOINT_SILENCE_S * 1000 / 80))
                assert heard[0] == SPEECH
                assert all(f == bytes(W.FRAME_BYTES) for f in heard[1:])
                await satellite.send(struct.pack('>BIH', em_listen.FRAME_TYPE, 42, 1) + SPEECH)
                await asyncio.sleep(.1)
                assert len(heard) == 1 + round(W.ENDPOINT_SILENCE_S * 1000 / 80)   # uplink closed
                assert d.mic_queue.empty() and d.voice_queue.empty()               # nothing left for the native path
        await d.workflow.stop()
    asyncio.run(main())
