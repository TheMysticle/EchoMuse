"""A Pipecat session leaves an Activity row, shaped like a native turn's, at no cost to the turn.

The row is what lets a Pipecat run be compared with the Home Assistant runs
already in the table: the same wake fields, the same stage columns, with speech
end to first reply audio as the number to read.
"""
import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

import em_db as db
import em_player
import em_workflow as W
from test_voice_workflow import device, until
import test_voice_workflow as workflow_fixtures

fresh_db = workflow_fixtures.fresh_db

WAKE = {"model": "alexa_v0.1", "score": 0.9031, "threshold": 0.864, "noise_floor": 0.003}


@pytest.fixture(autouse=True)
def _quiet_controller(monkeypatch):
    device()
    import em_controller as C
    monkeypatch.setattr(C, '_push_device_state', AsyncMock())
    monkeypatch.setattr(em_player, '_sessions', {})


def bridge():
    d = device()
    d.workflow.config = {'mode': W.PIPECAT, 'endpoint': 'ws://unused/api/assist/esphome'}
    d.workflow.status = 'connected'
    return d


def rows(d):
    return db.get_turns(d.device_id, 10)


def phase(name, **extra):
    return json.dumps({'type': 'phase', 'phase': name, **extra})


def test_a_session_nobody_spoke_in_is_a_no_speech_row_with_its_wake_details(fresh_db):
    async def main():
        d = bridge()
        try:
            assert await d.workflow.wake(1, wake_info=WAKE)
            d.workflow.session_deadline = 0
            await d.workflow.poll()
            await until(lambda: rows(d))
            row = rows(d)[0]
            assert row['outcome'] == 'no_speech'
            assert row['trigger'] == 'wakeword-dev(0.903) · pipecat (live)'
            assert (row['wake_model'], row['wake_score'], row['wake_threshold']) == ('alexa_v0.1', 0.9031, 0.864)
            assert row['vad_end_ms'] == -1 and row['stt_ms'] == -1 and row['playback_ms'] == -1
            assert row['total_ms'] >= 0
        finally:
            await d.workflow.stop()
    asyncio.run(main())


def test_the_wake_event_supplies_the_row_its_score_threshold_and_floor(fresh_db):
    async def main():
        d = bridge()
        try:
            ev = {"session": 1, "age_ms": 40, "score": 0.9031, "threshold": 0.864, "floor": 0.0031}
            assert await d.workflow_wake(ev)
            wake = d.workflow.turn["wake"]
            assert (wake["score"], wake["threshold"], wake["noise_floor"]) == (0.9031, 0.864, 0.0031)
            assert wake["model"]
        finally:
            await d.workflow.stop()
    asyncio.run(main())


def test_a_full_exchange_records_speech_end_first_audio_and_the_total_in_order(fresh_db):
    async def main():
        d = bridge()
        events = asyncio.Queue()
        ws = SimpleNamespace(recv=events.get, send=AsyncMock(), close=AsyncMock(), echomuse_audio_format=(24000, 1))
        receiver = asyncio.create_task(d.workflow.receive(ws))
        sender = asyncio.create_task(d.workflow.send(ws))
        try:
            await d.workflow.wake(1, wake_info=WAKE)
            await asyncio.sleep(.05)
            epoch = d.workflow.session_epoch
            d.workflow.queue.put_nowait((epoch, b'\0\0' * 160))
            d.workflow.queue.put_nowait((epoch, None))             # the Echo ended the utterance
            await asyncio.sleep(.1)
            assert d.workflow.turn["vad_end"] == -1                # its marker is not the end of speech
            await events.put(phase('thinking'))                    # the server's own speech-stop is
            await until(lambda: d.workflow.turn["vad_end"] >= 0)
            await asyncio.sleep(.05)
            await events.put((np.zeros(2400, dtype=np.int16)).tobytes())   # the reply begins
            await until(lambda: d.workflow.turn["first_audio"] >= 0)
            await events.put(phase('idle'))                         # and the conversation ends
            await until(lambda: rows(d) and d.turn_history)
            row = rows(d)[0]
            assert row['outcome'] == 'ok'
            assert 0 <= row['vad_end_ms'] < row['tts_url_ms'] == row['playback_ms'] <= row['total_ms']
            assert row['stt_ms'] == -1 and row['tts_fetch_ms'] == -1
            assert d.turn_history[-1]['total_ms'] == row['total_ms']
        finally:
            for task in (receiver, sender):
                task.cancel()
            await asyncio.gather(receiver, sender, return_exceptions=True)
            await d.workflow.stop()
    asyncio.run(main())


def test_ending_a_session_does_not_wait_for_the_row_to_be_written(fresh_db):
    # The next wake can arrive straight after an end; the database must not sit in its way.
    async def main():
        d = bridge()
        gate = asyncio.Event()
        written = []
        async def slow(record):
            await gate.wait()
            written.append(record)
        d.workflow_turn = slow
        try:
            await d.workflow.wake(1, wake_info=WAKE)
            await asyncio.wait_for(d.workflow.end_session(reason='conversation_ended'), 1)
            assert not written and d.workflow.turn_tasks          # still being written
            gate.set()
            await until(lambda: written)
            await until(lambda: not d.workflow.turn_tasks)
        finally:
            gate.set()
            await d.workflow.stop()
    asyncio.run(main())


def test_a_failed_write_is_logged_and_never_breaks_the_session(fresh_db, caplog):
    async def main():
        d = bridge()
        d.workflow_turn = AsyncMock(side_effect=RuntimeError('disk full'))
        try:
            await d.workflow.wake(1, wake_info=WAKE)
            await d.workflow.end_session(reason='conversation_ended')
            await until(lambda: not d.workflow.turn_tasks)
            assert not d.workflow.awake
        finally:
            await d.workflow.stop()
    with caplog.at_level('WARNING'):
        asyncio.run(main())
    assert 'could not record the Pipecat turn' in caplog.text


def test_a_button_turn_is_labelled_a_button_and_a_second_wake_records_the_first(fresh_db):
    async def main():
        d = bridge()
        try:
            await d.workflow.wake(1, wake_info=WAKE)
            await d.workflow.wake(2, wake_info={**WAKE, "score": 0.5})   # a new wake while awake ends the first
            await until(lambda: len(rows(d)) == 1)
            assert rows(d)[0]['wake_score'] == 0.9031
            await d.workflow.end_session(reason='conversation_ended')
            await until(lambda: len(rows(d)) == 2)
            assert sorted(r['wake_score'] for r in rows(d)) == [0.5, 0.9031]
            await d.workflow.button()
            await d.workflow.end_session(reason='conversation_ended')
            await until(lambda: len(rows(d)) == 3)
            assert any(r['trigger'] == 'button · pipecat (live)' for r in rows(d))
        finally:
            await d.workflow.stop()
    asyncio.run(main())


def test_a_device_without_the_hook_just_has_no_row(fresh_db):
    async def main():
        d = bridge()
        d.workflow_turn = None
        try:
            await d.workflow.wake(1, wake_info=WAKE)
            await d.workflow.end_session(reason='conversation_ended')
            assert not d.workflow.turn_tasks and not d.workflow.awake
        finally:
            await d.workflow.stop()
    asyncio.run(main())


def transcript(text, final=True, role='user'):
    return json.dumps({'type': 'transcript', 'role': role, 'final': final,
                       'text_b64': base64.b64encode(text.encode()).decode()})


def run_exchange(composed, user_events, then=()):
    """One session: wake, the Echo ends the utterance, user_events arrive, reply audio, end."""
    async def main():
        d = bridge()
        events = asyncio.Queue()
        ws = SimpleNamespace(recv=events.get, send=AsyncMock(), close=AsyncMock(),
                             echomuse_audio_format=(24000, 1), echomuse_device_endpoint=composed)
        d.workflow.ws = ws
        receiver = asyncio.create_task(d.workflow.receive(ws))
        try:
            await d.workflow.wake(1, wake_info=WAKE)
            await asyncio.sleep(.05)
            for event in user_events:
                await events.put(event)
                await asyncio.sleep(.03)
            await events.put(np.zeros(2400, dtype=np.int16).tobytes())
            await until(lambda: d.workflow.turn["first_audio"] >= 0)
            for event in then:
                await events.put(event)
                await asyncio.sleep(.03)
            await events.put(phase('idle'))
            await until(lambda: rows(d))
            return rows(d)[0]
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            await d.workflow.stop()
    return asyncio.run(main())


def test_a_composed_turn_records_when_streaming_stt_finished_and_what_it_heard(fresh_db):
    row = run_exchange(True, [phase('thinking'), transcript('turn on the lights')])
    assert row['trigger'].endswith('· pipecat (composed)')
    assert row['stt_text'] == 'turn on the lights'
    assert 0 <= row['vad_end_ms'] <= row['stt_ms'] < row['tts_url_ms'] <= row['total_ms']


def test_a_live_turn_keeps_the_text_but_has_no_stt_stage(fresh_db):
    # Gemini Live transcribes on the side; it is not a step before the model.
    row = run_exchange(False, [phase('thinking'), transcript('stop')])
    assert row['trigger'].endswith('· pipecat (live)')
    assert row['stt_text'] == 'stop' and row['stt_ms'] == -1
    assert row['vad_end_ms'] >= 0           # taken from the first thinking: the live route has no sentinel


def test_the_last_final_before_the_reply_is_the_text_and_later_words_are_ignored(fresh_db):
    row = run_exchange(True,
        [transcript('turn on', final=False), transcript('turn on the'), transcript('turn on the kitchen light')],
        then=[transcript('thank you')])
    assert row['stt_text'] == 'turn on the kitchen light'


def test_assistant_lines_bad_base64_and_interims_never_become_the_text(fresh_db):
    bad = json.dumps({'type': 'transcript', 'role': 'user', 'final': True, 'text_b64': '!!not base64!!'})
    row = run_exchange(True, [transcript('assistant words', role='assistant'),
                              transcript('half a sent', final=False), bad])
    assert row['stt_text'] == '' and row['stt_ms'] == -1


def test_a_transcript_event_changes_nothing_else_about_the_session(fresh_db):
    async def main():
        d = bridge()
        try:
            await d.workflow.wake(1, wake_info=WAKE)
            before = (d.workflow.accept_audio, d.workflow.awake, d.workflow.endpointed)
            await d.workflow.on_event(json.loads(transcript('hello')))
            assert (d.workflow.accept_audio, d.workflow.awake, d.workflow.endpointed) == before
            assert d.workflow.turn['stt_text'] == 'hello'
        finally:
            await d.workflow.stop()
    asyncio.run(main())


def test_a_streaming_stt_that_finishes_after_the_speech_stop_shows_its_own_stage(fresh_db):
    # speech stop, then the final words: transcribe is the gap between them.
    row = run_exchange(True, [phase('thinking'), transcript('turn on the lights')])
    assert row['stt_ms'] >= row['vad_end_ms'] >= 0
