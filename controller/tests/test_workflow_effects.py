"""Pipecat uses native media ownership, scenes, EQ and device drain semantics."""
import asyncio
import json
from types import MethodType
from unittest.mock import AsyncMock

import pytest

import em_player
import em_workflow as W
from test_voice_workflow import device, until
import test_voice_workflow as workflow_fixtures

fresh_db = workflow_fixtures.fresh_db


@pytest.mark.parametrize('finish', ['terminal', 'mute', 'switch', 'disconnect'])
def test_music_and_animated_speech_lifecycle(fresh_db, monkeypatch, finish):
    async def main():
        d = device()
        import em_controller as C
        d.capabilities = ['audio_mix', 'led_anim', 'output_chain', 'local_schedule']
        d._set_speaking = MethodType(C.Device._set_speaking, d)
        d.workflow.config = {'mode': W.PIPECAT}
        d.workflow.status = 'connected'
        monkeypatch.setattr(em_player, '_sessions', {})
        monkeypatch.setattr(em_player, '_get_device', lambda _: d)
        monkeypatch.setattr(C, '_push_device_state', AsyncMock())
        d.data_ws.send.side_effect = None  # explicitly simulate hardware drain
        try:
            assert await d.workflow.wake(42)
            session = em_player._session(d.device_id)
            assert session.owned_by_turn and session.duck_depth == 1
            assert d.voice_lock.locked() and d.listening
            await d.workflow.device.workflow_phase('thinking')
            await asyncio.sleep(0)
            d.workflow.output_pcm(b'\x12\x00' * 4800)
            d.workflow.finish_output('listening')
            await until(lambda: any(c.args[0] == b'\x03' for c in d.data_ws.send.call_args_list))
            assert d.speaking and d.speaker_busy == 1
            assert session.duck_depth == 1  # EOS on the socket is not hardware completion
            controls = [json.loads(c.args[0]) for c in d.control_ws.send.call_args_list]
            animations = [c['anim'] for c in controls if c['type'] == 'led_anim']
            assert any(c.get('pattern') == d.led_scene['spin_anim']['pattern'] for c in animations)
            assert any(c.get('pattern') == 'meter' for c in animations)
            assert {'type':'duck','on':True} in controls
            d.workflow.feed(b'\x01\x00'*1280,42)
            assert not d.workflow.queue.empty()  # live duplex through speaker drain
            if finish == 'terminal':
                closing = asyncio.create_task(d.workflow.end_session(drain=True))
                await until(lambda: not d.workflow.awake)
                d.workflow.feed(b'\x02\x00',42)
                assert d.workflow.queue.empty()
                assert not closing.done() and session.duck_depth == 1
                d.signal_playback_done()
                await closing
            elif finish == 'mute':
                d.muted = True
                await d.workflow.poll()
            elif finish == 'switch':
                await d.workflow.apply({'mode':W.NATIVE})
            else:
                await d.workflow.end_session(reason='pipecat_disconnected')
            assert not d.speaking and d.speaker_busy == 0
            assert not d.voice_lock.locked() and not session.owned_by_turn
            assert session.duck_depth == 0
            assert d.workflow.scene_task is None
            controls = [json.loads(c.args[0]) for c in d.control_ws.send.call_args_list]
            assert {'type':'duck','on':False} in controls
            if finish == 'terminal':
                assert not any(c['type']=='speaker_flush' for c in controls)
            d.control_ws.close.assert_not_called()
            d.data_ws.close.assert_not_called()
        finally:
            await d.workflow.stop()
    asyncio.run(main())


def test_interruption_cancels_response_without_releasing_conversation_music(fresh_db, monkeypatch):
    async def main():
        d = device()
        import em_controller as C
        d.capabilities = ['audio_mix','output_chain']
        d.workflow.config={'mode':W.PIPECAT}
        d.workflow.status='connected'
        monkeypatch.setattr(em_player,'_sessions',{})
        monkeypatch.setattr(em_player,'_get_device',lambda _:d)
        monkeypatch.setattr(C,'_push_device_state',AsyncMock())
        try:
            assert await d.workflow.wake(1)
            d.workflow.output_pcm(b'\x12\x00'*4800)
            await until(lambda:d.workflow.speaker_open)
            await d.workflow.interrupt()
            assert d.workflow.awake and d.listening
            assert em_player._session(d.device_id).duck_depth==1
            assert not d.workflow.playback_tasks and d.speaker_busy==0
            d.workflow.output_pcm(b'\x13\x00'*4800)
            d.workflow.finish_output()
            await until(lambda:not d.workflow.playback_tasks)
            await d.workflow.end_session()
            assert em_player._session(d.device_id).duck_depth==0
        finally:
            await d.workflow.stop()
    asyncio.run(main())


def test_pipecat_preserves_configured_wake_barge_in_setting():
    d=device()
    d.workflow.config={'mode':W.PIPECAT}
    wire=d.workflow.wire_config({'bargeInEnabled':False,'bargeInThreshold':.7,'owwOnDevice':'on'})
    assert wire['bargeInEnabled'] is False and wire['bargeInThreshold']==.7
    assert wire['owwOnDevice']=='on' and wire['aecEnabled'] is True


@pytest.mark.parametrize('question',[False,True])
def test_reply_returns_to_wake_standby_except_continuation_question(fresh_db,monkeypatch,question):
    async def main():
        from types import SimpleNamespace
        d=device()
        import em_controller as C
        monkeypatch.setattr(C,'_push_device_state',AsyncMock())
        monkeypatch.setattr(em_player,'_sessions',{})
        d.workflow.config={'mode':W.PIPECAT,'endpoint':'ws://unused/api/assist/esphome'}
        d.workflow.status='connected'
        events=asyncio.Queue()
        receiver=asyncio.create_task(d.workflow.receive(SimpleNamespace(recv=events.get,echomuse_audio_format=(24000,1))))
        try:
            await d.workflow.wake(1)
            d.workflow.feed(b'\x01\x00',1)
            await events.put(json.dumps({'type':'phase','phase':'thinking'}))
            await until(lambda:not d.workflow.accept_audio)
            assert d.workflow.queue.empty()
            d.workflow.feed(b'\x01\x00',1)
            assert d.workflow.queue.empty()  # background cannot barge into thinking
            await events.put(b'\x12\x00'*2400)
            await until(lambda:d.workflow.accept_audio)
            await events.put(json.dumps({'type':'phase','phase':'listening','follow_up':True,'continuation_question':question}))
            if question:
                await until(lambda:not d.workflow.playback_tasks)
                assert d.workflow.awake
                d.workflow.feed(b'\x02\x00',1)
                assert not d.workflow.queue.empty()
            else:
                await until(lambda:not d.workflow.awake and not d.workflow.media_owned)
                d.workflow.feed(b'\x02\x00',1)
                assert d.workflow.queue.empty()
                assert not d.voice_lock.locked()
        finally:
            receiver.cancel()
            await asyncio.gather(receiver,return_exceptions=True)
            await d.workflow.stop()
    asyncio.run(main())
