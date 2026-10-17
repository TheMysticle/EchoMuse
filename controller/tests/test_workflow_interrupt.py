"""What can interrupt a Pipecat reply: the Echo's wake word, or the server hearing you.

In wake mode the microphone is closed to the server while a reply is heard, so
the speaker's own voice cannot be taken for the user. These tests pin the gate,
that it reopens when the reply has been heard, that a wake still gets through,
and the Echoes where the setting cannot apply.
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
def _quiet(monkeypatch):
    device()
    import em_controller as C
    monkeypatch.setattr(C, '_push_device_state', AsyncMock())
    monkeypatch.setattr(em_player, '_sessions', {})


def bridge(private=True, **config):
    d = device()
    d.capabilities = ['output_chain', 'oww_local_only']
    d.oww_on_device = 'on' if private else 'off'
    d.listen_reported = 'local' if private else None
    d.workflow.config = {'mode': W.PIPECAT, 'endpoint': 'ws://unused/api/assist/esphome', **config}
    d.workflow.status = 'connected'
    d.data_ws.send.side_effect = None      # playback ends only when told
    return d


def serve(d):
    events = asyncio.Queue()
    ws = SimpleNamespace(recv=events.get, echomuse_audio_format=(24000, 1))
    return events, asyncio.create_task(d.workflow.receive(ws))


async def finish(d, receiver):
    receiver.cancel()
    await asyncio.gather(receiver, return_exceptions=True)
    await d.workflow.stop()


def test_wake_is_the_default_and_needs_an_echo_that_hears_its_own_wake_word():
    assert bridge(True).workflow.interrupt_effective == W.INTERRUPT_WAKE
    assert bridge(True, interrupt_by='voice').workflow.interrupt_effective == W.INTERRUPT_VOICE
    # An Echo streaming to the controller has nothing scoring a wake during a
    # session: closing its microphone would leave only the button.
    assert bridge(False).workflow.interrupt_effective == W.INTERRUPT_VOICE


def test_setting_is_validated_and_reported():
    assert W.validate({'mode': W.NATIVE}, {})['mode'] == W.NATIVE
    cfg = W.validate({'mode': W.PIPECAT, 'endpoint': 'ws://h:1', 'interrupt_by': 'voice'}, {})
    assert W.public(cfg)['interrupt_by'] == 'voice'
    assert W.public({'mode': W.NATIVE})['interrupt_by'] == 'wake'
    with pytest.raises(ValueError):
        W.validate({'mode': W.PIPECAT, 'endpoint': 'ws://h:1', 'interrupt_by': 'always'}, {})


def test_profile_defaults_to_the_one_that_suits_the_mode_and_a_stored_one_wins():
    # One-word commands are shorter than Balanced waits; echo cannot trip Responsive
    # when the microphone is closed during replies.
    assert W.barge_profile({}) == 'responsive'
    assert W.barge_profile({'interrupt_by': 'voice'}) == 'balanced'
    assert W.barge_profile({'interrupt_by': 'wake', 'barge_in_sensitivity': 'low'}) == 'low'
    assert W.barge_profile({'interrupt_by': 'voice', 'barge_in_sensitivity': 'responsive'}) == 'responsive'


def test_server_hears_nothing_while_a_reply_plays_in_wake_mode(fresh_db):
    async def main():
        d = bridge()
        events, receiver = serve(d)
        try:
            assert await d.workflow.wake(1)
            d.workflow.feed(b'\x01\x00', 1)
            assert not d.workflow.queue.empty()                   # listening: open
            await events.put(b'\x10\x00' * 2400)
            await until(lambda: d.workflow.reply_gated)
            assert d.workflow.queue.empty()                       # pre-reply audio discarded
            d.workflow.feed(b'\x02\x00', 1)
            assert d.workflow.queue.empty()                       # and nothing new accepted
        finally:
            await finish(d, receiver)
    asyncio.run(main())


def test_voice_mode_keeps_the_microphone_open_under_a_reply(fresh_db):
    async def main():
        d = bridge(interrupt_by='voice')
        events, receiver = serve(d)
        try:
            await d.workflow.wake(1)
            await events.put(b'\x10\x00' * 2400)
            await until(lambda: d.workflow.output_queue is not None)
            assert not d.workflow.reply_gated
            d.workflow.feed(b'\x02\x00', 1)
            assert not d.workflow.queue.empty()
        finally:
            await finish(d, receiver)
    asyncio.run(main())


def test_microphone_reopens_for_a_clarification_only_once_the_reply_is_heard(fresh_db):
    async def main():
        d = bridge()
        events, receiver = serve(d)
        try:
            await d.workflow.wake(1)
            await events.put(b'\x10\x00' * 2400)
            await events.put(json.dumps({'type': 'phase', 'phase': 'listening', 'follow_up': True,
                                         'continuation_question': True}))
            await until(lambda: d.workflow.draining)
            assert d.workflow.accept_audio and d.workflow.reply_gated   # server is ready; user cannot answer yet
            d.workflow.feed(b'\x03\x00', 1)
            assert d.workflow.queue.empty()
            await until(lambda: any(c.args[0] == b'\x03' for c in d.data_ws.send.call_args_list))
            d.signal_playback_done()
            await until(lambda: not d.workflow.playback_tasks)
            assert not d.workflow.reply_gated and d.workflow.awake
            d.workflow.feed(b'\x04\x00', 1)
            assert not d.workflow.queue.empty()
        finally:
            await finish(d, receiver)
    asyncio.run(main())


def test_a_wake_word_during_a_reply_interrupts_it_and_opens_a_new_session(fresh_db):
    async def main():
        d = bridge()
        events, receiver = serve(d)
        try:
            await d.workflow.wake(1)
            await events.put(b'\x10\x00' * 2400)
            await until(lambda: d.workflow.reply_gated)
            assert await d.workflow.wake(2)
            assert d.workflow.session == 2 and d.workflow.awake
            assert not d.workflow.reply_gated                       # the new session is listening
            d.workflow.feed(b'\x05\x00', 2)
            assert not d.workflow.queue.empty()
        finally:
            await finish(d, receiver)
    asyncio.run(main())
