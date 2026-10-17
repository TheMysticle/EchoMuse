"""When a Pipecat session ends, and what its reply audio sounds like.

Each test names a way a satellite used to stay open too long, end too early, or
crackle. Time is driven by setting the bridge's deadline, never by sleeping.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
from scipy.signal import resample_poly

import em_player
import em_workflow as W
from test_voice_workflow import device, until
import test_voice_workflow as workflow_fixtures

fresh_db = workflow_fixtures.fresh_db


@pytest.fixture(autouse=True)
def _quiet_controller(monkeypatch):
    device()                       # imports em_controller behind the openwakeword stub
    import em_controller as C
    monkeypatch.setattr(C, '_push_device_state', AsyncMock())
    monkeypatch.setattr(em_player, '_sessions', {})


def awake_bridge(raw_pcm=True):
    d = device()
    d.workflow.config = {'mode': W.PIPECAT, 'endpoint': 'ws://unused/api/assist/esphome'}
    d.workflow.status = 'connected'
    return d


def serve_events(d, rate=24000):
    events = asyncio.Queue()
    ws = SimpleNamespace(recv=events.get, echomuse_audio_format=(rate, 1))
    return events, asyncio.create_task(d.workflow.receive(ws))


async def stop(d, receiver):
    receiver.cancel()
    await asyncio.gather(receiver, return_exceptions=True)
    await d.workflow.stop()


def phase(name, **extra):
    return json.dumps({'type': 'phase', 'phase': name, **extra})


def test_silence_after_a_wake_ends_the_session_in_the_listen_window(fresh_db):
    async def main():
        d = awake_bridge()
        try:
            assert await d.workflow.wake(1)
            now = asyncio.get_running_loop().time()
            assert 0 < d.workflow.session_deadline - now <= W.LISTEN_IDLE_S
            assert d.workflow.deadline_reason == 'listen_timeout'
            d.workflow.session_deadline = 0
            await d.workflow.poll()
            assert not d.workflow.awake and not d.voice_lock.locked()
        finally:
            await d.workflow.stop()
    asyncio.run(main())


def test_thinking_swaps_the_short_listen_window_for_the_long_think_window(fresh_db):
    # Without this the 8s listen window, armed by the last transcript, would end
    # a session whose model is still working on a tool call.
    async def main():
        d = awake_bridge()
        events, receiver = serve_events(d)
        try:
            await d.workflow.wake(1)
            await events.put(phase('thinking'))
            await until(lambda: not d.workflow.accept_audio)
            await until(lambda: d.workflow.deadline_reason == 'think_timeout')
            remaining = d.workflow.session_deadline - asyncio.get_running_loop().time()
            assert W.LISTEN_IDLE_S < remaining <= W.THINK_IDLE_S
        finally:
            await stop(d, receiver)
    asyncio.run(main())


def test_transcripts_keep_a_long_command_open(fresh_db):
    async def main():
        d = awake_bridge()
        events, receiver = serve_events(d)
        try:
            await d.workflow.wake(1)
            d.workflow.session_deadline = asyncio.get_running_loop().time() + .05
            await events.put(json.dumps({'type': 'transcript', 'role': 'user', 'final': False, 'text_b64': 'eA=='}))
            await asyncio.sleep(.05)
            await until(lambda: d.workflow.session_deadline - asyncio.get_running_loop().time() > W.LISTEN_IDLE_S - 1)
            await d.workflow.poll()
            assert d.workflow.awake
        finally:
            await stop(d, receiver)
    asyncio.run(main())


def test_session_is_never_longer_than_the_hard_cap(fresh_db):
    async def main():
        d = awake_bridge()
        try:
            await d.workflow.wake(1)
            d.workflow.arm(W.THINK_IDLE_S, 'think_timeout')
            d.workflow.session_hard_deadline = 1
            await d.workflow.poll()
            assert not d.workflow.awake
        finally:
            await d.workflow.stop()
    asyncio.run(main())


def test_a_pause_inside_a_reply_does_not_end_the_session(fresh_db):
    # The old 5s receive timeout ended the session, and dropped the rest of the
    # reply, whenever a tool call or a slow model paused mid-sentence.
    async def main():
        d = awake_bridge()
        events, receiver = serve_events(d)
        try:
            await d.workflow.wake(1)
            await events.put(b'\x10\x00' * 2400)
            await until(lambda: d.workflow.output_queue is not None)
            await asyncio.sleep(.1)
            assert d.workflow.awake and d.workflow.output_queue is not None
            await events.put(b'\x10\x00' * 2400)
            await events.put(phase('listening', follow_up=True, continuation_question=False))
            await until(lambda: not d.workflow.awake)
        finally:
            await stop(d, receiver)
    asyncio.run(main())


def test_a_server_that_goes_quiet_mid_reply_is_drained_not_cut(fresh_db):
    async def main():
        d = awake_bridge()
        events, receiver = serve_events(d)
        try:
            await d.workflow.wake(1)
            await events.put(b'\x10\x00' * 2400)
            await until(lambda: d.workflow.output_queue is not None)
            d.workflow.session_deadline = 0
            assert d.workflow.deadline_reason == 'reply_stalled'
            closing = asyncio.create_task(d.workflow.poll())
            await until(lambda: not d.workflow.awake)
            assert d.workflow.output_queue is None   # sentinel sent: the audio plays out
            d.signal_playback_done()
            await closing
        finally:
            await stop(d, receiver)
    asyncio.run(main())


def test_idle_window_waits_while_the_speaker_plays_what_was_already_received(fresh_db):
    # The server finishes sending long before a paced speaker finishes playing.
    async def main():
        d = awake_bridge()
        try:
            await d.workflow.wake(1)
            d.workflow.output_pcm(b'\x10\x00' * 4800)
            d.workflow.finish_output('listening')
            assert d.workflow.draining
            d.workflow.session_deadline = 0
            await d.workflow.poll()
            assert d.workflow.awake
        finally:
            await d.workflow.stop()
    asyncio.run(main())


def test_clarification_window_opens_when_the_reply_has_been_heard(fresh_db):
    async def main():
        d = awake_bridge()
        d.data_ws.send.side_effect = None            # the speaker reports done only when told
        events, receiver = serve_events(d)
        try:
            await d.workflow.wake(1)
            await events.put(b'\x10\x00' * 2400)
            await until(lambda: d.workflow.output_queue is not None)
            await events.put(phase('listening', follow_up=True, continuation_question=True))
            await until(lambda: d.workflow.draining)
            await until(lambda: any(c.args[0] == b'\x03' for c in d.data_ws.send.call_args_list))
            d.workflow.session_deadline = 0           # what arming at send time would have left
            await d.workflow.poll()
            assert d.workflow.awake                    # still playing: not the user's turn yet
            d.signal_playback_done()
            await until(lambda: not d.workflow.playback_tasks)
            remaining = d.workflow.session_deadline - asyncio.get_running_loop().time()
            assert 0 < remaining <= W.LISTEN_IDLE_S and d.workflow.awake
            assert d.workflow.accept_audio             # microphone open for the answer
        finally:
            await stop(d, receiver)
    asyncio.run(main())


def test_every_ended_session_says_why(fresh_db, caplog):
    async def main():
        d = awake_bridge()
        try:
            await d.workflow.wake(1)
            d.workflow.session_deadline = 0
            with caplog.at_level('INFO', logger='echomuse.workflow'):
                await d.workflow.poll()
        finally:
            await d.workflow.stop()
    asyncio.run(main())
    assert any('listen_timeout' in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize('rate,chunk_ms', [(24000, 20), (24000, 40), (16000, 20), (44100, 23), (22050, 10)])
def test_reply_audio_reaches_the_speaker_without_seams(fresh_db, rate, chunk_ms):
    # What the speaker is given must equal a one-shot conversion of the same
    # reply, however the server happened to cut it into messages.
    async def main():
        d = awake_bridge()
        events, receiver = serve_events(d, rate)
        queued = []
        try:
            await d.workflow.wake(1)
            t = np.arange(rate) / rate
            reply = (np.sin(2 * np.pi * 440 * t) * 12000).astype('<i2')
            step = rate * chunk_ms // 1000
            original = d.workflow.output_pcm
            d.workflow.output_pcm = lambda pcm: (queued.append(pcm), original(pcm))[1]
            for i in range(0, reply.size, step):
                await events.put(reply[i:i + step].tobytes())
            await events.put(phase('listening', follow_up=True, continuation_question=False))
            await until(lambda: not d.workflow.awake)
            # the reply tail is flushed when the reply ends
            got = np.frombuffer(b''.join(queued), dtype='<i2').astype(float)
            return reply, got
        finally:
            await stop(d, receiver)
    reply, got = asyncio.run(main())
    import math
    g = math.gcd(rate, 48000)
    want = np.rint(resample_poly(reply.astype(float), 48000 // g, rate // g))
    n = min(got.size, want.size)
    assert n >= want.size - 48000 * 0.02          # nearly all of it, tail may still be queued
    assert np.max(np.abs(got[:n] - want[:n])) <= 1


def test_a_burst_of_small_messages_is_not_a_protocol_error(fresh_db):
    # 128 messages of 20ms used to be the ceiling; a model can send 10s of
    # speech in a burst while the paced speaker takes it at real time.
    async def main():
        d = awake_bridge()
        try:
            await d.workflow.wake(1)
            for _ in range(600):
                d.workflow.output_pcm(b'\x01\x00' * 960)
            assert d.workflow.queued_bytes == 600 * 1920
            with pytest.raises(W.ProtocolError):
                d.workflow.output_pcm(b'\x01\x00' * (W.MAX_REPLY_SECONDS * W.SPEAKER_RATE))
        finally:
            await d.workflow.stop()
    asyncio.run(main())


def test_thinking_just_after_reply_audio_does_not_split_the_reply(fresh_db):
    # Field log 2026-10-18: a 152ms stream, then a second stream that waited
    # 1.2s to prime, so a reply began with one word and a second of silence.
    async def main():
        d = awake_bridge()
        events, receiver = serve_events(d)
        try:
            await d.workflow.wake(1)
            w = d.workflow
            pcm = np.zeros(2400, dtype='<i2').tobytes()
            w.output_pcm(pcm)
            first_queue = w.output_queue
            await events.put(phase('thinking'))
            await asyncio.sleep(.05)
            assert w.output_queue is first_queue and w.pending_phase is None
            w.output_pcm(pcm)
            assert w.output_queue is first_queue
        finally:
            await stop(d, receiver)
    asyncio.run(main())


def test_thinking_after_a_real_pause_in_reply_audio_still_ends_the_stream(fresh_db):
    async def main():
        d = awake_bridge()
        events, receiver = serve_events(d)
        try:
            await d.workflow.wake(1)
            w = d.workflow
            w.output_pcm(np.zeros(2400, dtype='<i2').tobytes())
            w.last_reply_pcm -= W.THINK_SPLIT_S + .5
            await events.put(phase('thinking'))
            await until(lambda: w.output_queue is None)
        finally:
            await stop(d, receiver)
    asyncio.run(main())
