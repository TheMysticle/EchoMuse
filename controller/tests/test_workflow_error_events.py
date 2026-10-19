"""A recoverable Pipecat error must not cost the connection.

2026-10-19: Cartesia closed an idle socket in the instant a reply was sent, the
add-on reported an error, and the controller dropped the whole connection and
ended the turn, so an answer that had been written was never spoken.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import em_player
import em_workflow as W
from test_voice_workflow import device, until
import test_voice_workflow as workflow_fixtures

fresh_db = workflow_fixtures.fresh_db


@pytest.fixture(autouse=True)
def _quiet_controller(monkeypatch):
    device()
    import em_controller as C
    monkeypatch.setattr(C, '_push_device_state', AsyncMock())
    monkeypatch.setattr(em_player, '_sessions', {})


def error(recoverable, code='pipeline_error'):
    return json.dumps({'type': 'error', 'code': code, 'message_b64': '', 'recoverable': recoverable})


def serve(d):
    events = asyncio.Queue()
    ws = SimpleNamespace(recv=events.get, send=AsyncMock(), close=AsyncMock(), echomuse_audio_format=(24000, 1))
    d.workflow.config = {'mode': W.PIPECAT, 'endpoint': 'ws://unused/api/assist/esphome'}
    d.workflow.status = 'connected'
    return events, ws, asyncio.create_task(d.workflow.receive(ws))


def test_a_recoverable_error_keeps_the_connection_and_gives_the_reply_a_short_window(fresh_db):
    async def main():
        d = device()
        events, ws, receiver = serve(d)
        try:
            await d.workflow.wake(1)
            await events.put(error(True))
            await until(lambda: d.workflow.deadline_reason == 'error_recovery')
            assert not receiver.done()                                   # the connection is still being read
            now = asyncio.get_running_loop().time()
            assert 0 < d.workflow.session_deadline - now <= W.ERROR_AUDIO_S
            assert d.workflow.awake
            d.workflow.session_deadline = 0                              # no reply came
            await d.workflow.poll()
            assert not d.workflow.awake and not receiver.done()          # the turn ended, the connection did not
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            await d.workflow.stop()
    asyncio.run(main())


@pytest.mark.parametrize("payload", [error(False), json.dumps({'type': 'error', 'code': 'x'})])
def test_a_fatal_or_unlabelled_error_still_drops_the_connection(fresh_db, payload):
    async def main():
        d = device()
        events, ws, receiver = serve(d)
        await events.put(payload)
        with pytest.raises(W.ProtocolError):
            await asyncio.wait_for(receiver, 2)
    asyncio.run(main())


def test_a_late_reply_after_a_recoverable_error_is_still_played(fresh_db):
    async def main():
        d = device()
        events, ws, receiver = serve(d)
        try:
            await d.workflow.wake(1)
            await events.put(error(True))
            await until(lambda: d.workflow.deadline_reason == 'error_recovery')
            await events.put(b'\0\0' * 2400)                              # the re-sent text arrives
            await until(lambda: d.workflow.speaking)
            assert d.workflow.awake and not receiver.done()
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            await d.workflow.stop()
    asyncio.run(main())


def test_repeated_recoverable_errors_are_not_recovery_and_drop_the_connection(fresh_db):
    # A pipeline that keeps failing must be rebuilt, as every error used to do.
    async def main():
        d = device()
        events, ws, receiver = serve(d)
        for _ in range(W.ERROR_BURST_COUNT):
            await events.put(error(True))
        with pytest.raises(W.ProtocolError):
            await asyncio.wait_for(receiver, 2)
    asyncio.run(main())


def test_errors_spread_over_time_do_not_add_up(fresh_db):
    async def main():
        d = device()
        events, ws, receiver = serve(d)
        try:
            for _ in range(W.ERROR_BURST_COUNT + 2):
                await events.put(error(True))
                await asyncio.sleep(.05)
                d.workflow.error_times = [t - W.ERROR_BURST_S - 1 for t in d.workflow.error_times]   # each one a minute apart
            assert not receiver.done()
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
    asyncio.run(main())


def test_a_connection_warms_the_resampler_in_a_thread_without_blocking_the_loop(fresh_db, monkeypatch):
    import threading
    import time

    import em_resample
    seen = {}

    def slow_warm():
        seen['thread'] = threading.current_thread() is not threading.main_thread()
        time.sleep(0.3)

    monkeypatch.setattr(em_resample, 'warmed', lambda: False)
    monkeypatch.setattr(em_resample, 'warm', slow_warm)

    async def main():
        d = device()
        started = time.monotonic()
        d.workflow._warm_resampler()
        assert time.monotonic() - started < 0.1            # returned at once
        await until(lambda: 'thread' in seen)
        await asyncio.gather(*list(d.workflow.turn_tasks))
        assert seen['thread'] and not d.workflow.turn_tasks
    asyncio.run(main())


def test_an_already_warm_resampler_schedules_nothing(fresh_db, monkeypatch):
    import em_resample
    monkeypatch.setattr(em_resample, 'warmed', lambda: True)

    async def main():
        d = device()
        d.workflow._warm_resampler()
        assert not d.workflow.turn_tasks
    asyncio.run(main())
