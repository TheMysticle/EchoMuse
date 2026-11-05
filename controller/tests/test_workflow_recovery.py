"""Active-room ownership, late reply suppression and local timeout recovery."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import em_state
import em_workflow as W
from test_voice_workflow import device, until, fresh_db


def configured(name, area='Living Room'):
    d = device()
    d.device_id = name
    d.workflow.config = {'mode': W.PIPECAT, 'area': area, 'endpoint': 'ws://unused/api/assist/esphome'}
    d.workflow.status = 'connected'
    d.workflow.ws = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
    return d


def test_room_session_owns_repeated_wakes_but_not_other_rooms(fresh_db, monkeypatch):
    async def run():
        a, b, c = configured('a'), configured('b', 'living_room'), configured('c', 'Kitchen')
        monkeypatch.setattr(em_state, 'devices', {d.device_id: d for d in (a,b,c)})
        import em_controller as C
        monkeypatch.setattr(C, '_push_device_state', AsyncMock())
        try:
            assert await a.workflow.wake(1)
            assert not await b.workflow.wake(1)
            assert await c.workflow.wake(1)
            # Existing owner still accepts its own barge.
            assert await a.workflow.wake(2)
            await a.workflow.end_session()
            assert await b.workflow.wake(2)
        finally:
            for d in (a,b,c):
                await d.workflow.stop()
    asyncio.run(run())


def test_simultaneous_room_wakes_reserve_before_speaker_lock(fresh_db, monkeypatch):
    async def run():
        a, b = configured('a'), configured('b')
        monkeypatch.setattr(em_state, 'devices', {'a': a, 'b': b})
        import em_controller as C
        monkeypatch.setattr(C, '_push_device_state', AsyncMock())
        await a.voice_lock.acquire()
        task = asyncio.create_task(a.workflow.wake(1))
        try:
            await until(lambda: a.workflow.opening)
            assert not await b.workflow.wake(1)
            a.voice_lock.release()
            assert await task
        finally:
            await a.workflow.stop()
            await b.workflow.stop()
    asyncio.run(run())


def test_missing_room_never_groups_unassigned_devices(fresh_db):
    a, b = configured('a', ''), configured('b', '')
    b.workflow.awake = True
    assert W.active_room_session(a) is None


def test_stale_reply_cannot_end_new_wake_before_its_speech(fresh_db, monkeypatch):
    async def run():
        d = configured('a')
        import em_controller as C
        monkeypatch.setattr(C, '_push_device_state', AsyncMock())
        d.workflow.ws.echomuse_turn_events = True
        try:
            assert await d.workflow.wake(1)
            await d.workflow.on_event({'phase': 'listening', 'follow_up': True})
            assert d.workflow.awake and d.workflow.awaiting_user_turn
            await d.workflow.on_event({'phase': 'listening', 'user_started': True})
            await d.workflow.on_event({'phase': 'thanks', 'terminal': True})
            assert d.workflow.awake
            await d.workflow.on_event({'phase': 'thinking', 'user_stopped': True})
            assert not d.workflow.awaiting_user_turn
            await d.workflow.on_event({'phase': 'thanks', 'terminal': True})
            assert not d.workflow.awake
        finally:
            await d.workflow.stop()
    asyncio.run(run())


def test_timeout_plays_bundled_audio_then_recycles(fresh_db, monkeypatch):
    async def run():
        d = configured('a')
        import em_controller as C
        monkeypatch.setattr(C, '_push_device_state', AsyncMock())
        d.workflow.output_pcm = lambda pcm: played.append(pcm)
        played = []
        ws = SimpleNamespace(recv=AsyncMock(return_value=json.dumps({
            'type': 'error', 'code': 'response_timeout', 'recoverable': False})))
        try:
            assert await d.workflow.wake(1)
            with pytest.raises(W.ProtocolError, match='response_timeout'):
                await d.workflow.receive(ws)
            assert len(played) == 1 and len(played[0]) > 48000
            assert not d.workflow.awake and not d.voice_lock.locked()
        finally:
            await d.workflow.stop()
    asyncio.run(run())
