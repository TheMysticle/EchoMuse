"""Echoes that share a conversation (docs/voice-workflow.md).

One wake heard by two Echoes answers once; a wake on one while the other is
thinking or answering takes the conversation over; each tells the others
what was said, and only servers that asked (`peer_context`) are told.
"""
import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import em_state
import em_workflow as W
from test_voice_workflow import device, fresh_db


def configured(name, conversation='Living room', area=''):
    d = device()
    d.device_id = name
    d.workflow.config = {'mode': W.PIPECAT, 'area': area, 'conversation': conversation,
                         'endpoint': 'ws://unused/api/assist/esphome'}
    d.workflow.status = 'connected'
    d.workflow.ws = SimpleNamespace(send=AsyncMock(), close=AsyncMock(), echomuse_peer_context=True)
    return d


def fleet(monkeypatch, *devices):
    monkeypatch.setattr(em_state, 'devices', {d.device_id: d for d in devices})
    import em_controller as C
    monkeypatch.setattr(C, '_push_device_state', AsyncMock())


def sent_types(d):
    return [json.loads(c.args[0])['type'] for c in d.workflow.ws.send.await_args_list
            if isinstance(c.args[0], str)]


@pytest.mark.parametrize('name', ['Living room', '  living   ROOM ', 'Salon ☕', 'x' * W.CONVERSATION_MAX, ''])
def test_a_conversation_name_is_accepted(name):
    assert W.validate({'mode': W.PIPECAT, 'endpoint': 'ws://h:1', 'conversation': name}, {})['conversation'] == name


@pytest.mark.parametrize('name', [5, None, ['a'], 'x' * (W.CONVERSATION_MAX + 1), 'a\nb', 'a\tb', 'a\x7fb', '\x00'])
def test_a_bad_conversation_name_is_refused(name):
    with pytest.raises(ValueError, match='conversation'):
        W.validate({'mode': W.PIPECAT, 'endpoint': 'ws://h:1', 'conversation': name}, {})


def test_names_match_by_case_and_spacing_and_only_in_pipecat_mode():
    assert W.conversation_key({'mode': W.PIPECAT, 'conversation': '  Living   Room '}) == 'living room'
    assert W.conversation_key({'mode': W.NATIVE, 'conversation': 'Living room'}) == ''
    assert W.conversation_key({'mode': W.PIPECAT}) == ''
    assert W.conversation_key(None) == ''


def test_peers_are_the_other_connected_echoes_with_the_same_name(fresh_db, monkeypatch):
    a, b, c, d = configured('a'), configured('b', 'LIVING ROOM'), configured('c', 'Kitchen'), configured('d', '')
    fleet(monkeypatch, a, b, c, d)
    assert W.conversation_peers(a) == [b]
    assert W.conversation_peers(c) == []
    assert W.conversation_peers(d) == []
    b.workflow.config = {**b.workflow.config, 'mode': W.NATIVE}
    assert W.conversation_peers(a) == []
    del em_state.devices['b']
    assert W.conversation_peers(a) == []


def test_a_wake_while_the_other_is_listening_gives_way(fresh_db, monkeypatch):
    async def run():
        a, b = configured('a'), configured('b')
        fleet(monkeypatch, a, b)
        try:
            assert await a.workflow.wake(1)
            assert a.workflow.capturing
            assert not await b.workflow.wake(7)
            assert a.workflow.awake and not b.workflow.awake
            # A button press is a deliberate choice of this Echo, never a copy.
            assert await b.workflow.wake(None, button=True) is not False
        finally:
            await a.workflow.stop()
            await b.workflow.stop()
    asyncio.run(run())


def test_a_wake_while_the_other_is_thinking_takes_the_conversation_over(fresh_db, monkeypatch):
    async def run():
        a, b = configured('a'), configured('b')
        fleet(monkeypatch, a, b)
        try:
            assert await a.workflow.wake(1)
            a.workflow.endpointed = True          # the talker finished: thinking
            assert not a.workflow.capturing
            assert await b.workflow.wake(2)
            assert b.workflow.awake and not a.workflow.awake
            assert 'stop' in sent_types(a) or 'end' in sent_types(a)
        finally:
            await a.workflow.stop()
            await b.workflow.stop()
    asyncio.run(run())


def test_a_wake_while_the_other_plays_its_reply_cuts_the_reply(fresh_db, monkeypatch):
    async def run():
        a, b = configured('a'), configured('b')
        fleet(monkeypatch, a, b)
        cut = AsyncMock()
        monkeypatch.setattr(a.workflow, 'cancel_output', cut)
        monkeypatch.setattr(type(a.workflow), 'draining', property(lambda self: self is a.workflow))
        try:
            assert await b.workflow.wake(2)
            cut.assert_awaited_once_with(force=True)
        finally:
            await a.workflow.stop()
            await b.workflow.stop()
    asyncio.run(run())


def test_without_a_name_nothing_changes(fresh_db, monkeypatch):
    async def run():
        a, b = configured('a', ''), configured('b', '')
        fleet(monkeypatch, a, b)
        try:
            assert await a.workflow.wake(1)
            assert await b.workflow.wake(1)        # no room, no name: independent
            assert a.workflow.awake and b.workflow.awake
        finally:
            await a.workflow.stop()
            await b.workflow.stop()
    asyncio.run(run())


def test_simultaneous_wakes_in_one_conversation_open_only_one(fresh_db, monkeypatch):
    async def run():
        a, b = configured('a'), configured('b')
        fleet(monkeypatch, a, b)
        await a.voice_lock.acquire()
        task = asyncio.create_task(a.workflow.wake(1))
        try:
            await asyncio.sleep(0.05)
            assert a.workflow.opening
            assert not await b.workflow.wake(1)
            a.voice_lock.release()
            assert await task
        finally:
            await a.workflow.stop()
            await b.workflow.stop()
    asyncio.run(run())


def event(role, text, final=True):
    return {'type': 'transcript', 'role': role, 'final': final,
            'text_b64': base64.b64encode(text.encode()).decode()}


def test_each_exchange_of_a_session_is_kept_in_order():
    turn = {}
    for ev in (event('user', 'what', False), event('user', 'what is on the list'),
               event('assistant', 'Milk', False), event('assistant', 'Milk and eggs'),
               event('user', 'add bread'), event('assistant', 'Added.')):
        W.note_exchange(turn, ev)
    assert turn['exchanges'] == [['what is on the list', 'Milk and eggs'], ['add bread', 'Added.']]


def test_the_exchange_goes_only_to_peers_whose_server_asked(fresh_db, monkeypatch):
    async def run():
        a, b, c, k = configured('a'), configured('b'), configured('c'), configured('k', 'Kitchen')
        c.workflow.ws.echomuse_peer_context = False
        fleet(monkeypatch, a, b, c, k)
        n = await W.share_exchanges(a, [['Która godzina?', 'Piętnasta.']])
        assert n == 1
        [call] = b.workflow.ws.send.await_args_list
        msg = json.loads(call.args[0])
        assert msg['type'] == 'peer_context'
        assert base64.b64decode(msg['user_b64']).decode() == 'Która godzina?'
        assert base64.b64decode(msg['reply_b64']).decode() == 'Piętnasta.'
        assert c.workflow.ws.send.await_count == 0 and k.workflow.ws.send.await_count == 0
        assert await W.share_exchanges(a, [['', 'reply only']]) == 0
        assert await W.share_exchanges(a, []) == 0
    asyncio.run(run())


def test_a_send_failure_to_one_peer_does_not_stop_the_others(fresh_db, monkeypatch):
    async def run():
        a, b, c = configured('a'), configured('b'), configured('c')
        b.workflow.ws.send.side_effect = ConnectionError('gone')
        fleet(monkeypatch, a, b, c)
        assert await W.share_exchanges(a, [['hi', 'hello']]) == 1
    asyncio.run(run())


def test_texts_are_bounded_and_only_the_last_exchanges_go(fresh_db, monkeypatch):
    async def run():
        a, b = configured('a'), configured('b')
        fleet(monkeypatch, a, b)
        exchanges = [[f'q{i}', 'r'] for i in range(6)] + [['u' * 900, 'x' * 2000]]
        assert await W.share_exchanges(a, exchanges) == W.SHARED_EXCHANGES_MAX
        last = json.loads(b.workflow.ws.send.await_args_list[-1].args[0])
        assert len(base64.b64decode(last['user_b64']).decode()) == W.SHARED_USER_MAX
        assert len(base64.b64decode(last['reply_b64']).decode()) == W.SHARED_REPLY_MAX
    asyncio.run(run())


def test_ending_a_session_shares_what_was_said(fresh_db, monkeypatch):
    async def run():
        a, b = configured('a'), configured('b')
        fleet(monkeypatch, a, b)
        try:
            assert await a.workflow.wake(1)
            a.workflow.note_transcript(event('user', 'What is on the list?'))
            a.workflow.note_transcript(event('assistant', 'Milk.'))
            await a.workflow.end_session()
            for _ in range(20):
                if b.workflow.ws.send.await_count:
                    break
                await asyncio.sleep(0.01)
            assert sent_types(b) == ['peer_context']
        finally:
            await a.workflow.stop()
            await b.workflow.stop()
    asyncio.run(run())


def test_an_unshared_echo_keeps_no_exchanges(fresh_db, monkeypatch):
    async def run():
        a = configured('a', '')
        fleet(monkeypatch, a)
        try:
            assert await a.workflow.wake(1)
            a.workflow.note_transcript(event('user', 'hello'))
            assert 'exchanges' not in a.workflow.turn
        finally:
            await a.workflow.stop()
    asyncio.run(run())



@pytest.mark.parametrize('flag, want', [(True, True), ('yes', False), (None, False)])
def test_the_handshake_reads_the_peer_context_flag_strictly(fresh_db, flag, want):
    from websockets.asyncio.server import serve
    from test_voice_workflow import va_hello

    async def main():
        async def server(ws):
            hello = va_hello()
            if flag is not None:
                hello['echomuse'] = {'peer_context': flag}
            await ws.send(json.dumps(hello))
            await ws.wait_closed()
        async with serve(server, '127.0.0.1', 0) as host:
            cfg = {'mode': W.PIPECAT,
                   'endpoint': f'ws://127.0.0.1:{host.sockets[0].getsockname()[1]}/api/assist/esphome'}
            ws = await W.handshake(cfg, 'satellite')
            try:
                return ws.echomuse_peer_context
            finally:
                await ws.close()
    assert asyncio.run(main()) is want


def test_the_device_wake_path_gives_way_before_arbitration(fresh_db, monkeypatch):
    async def run():
        import em_controller as C
        a, b = configured('a'), configured('b')
        fleet(monkeypatch, a, b)
        claim = AsyncMock(return_value='b')
        monkeypatch.setattr(C, '_claim_wake', claim)
        b.listen_close = AsyncMock()
        monkeypatch.setattr(C, 'leds_off', AsyncMock())
        try:
            assert await a.workflow.wake(1)
            ev = {'session': 9, 'age_ms': 0, 'score': 0.9, 'threshold': 0.8, 'floor': 0.001, 'arrived': 0.0}
            assert await b.workflow_wake(ev) is False
            b.listen_close.assert_awaited_once()
            assert b.listen_close.await_args.args == (9, 'ceded')
            claim.assert_not_called()           # no arbitration wait for a copy
            assert not b.workflow.awake
        finally:
            await a.workflow.stop()
            await b.workflow.stop()
    asyncio.run(run())


def test_the_device_wake_path_arbitrates_when_the_other_is_thinking(fresh_db, monkeypatch):
    async def run():
        import em_controller as C
        a, b = configured('a'), configured('b')
        fleet(monkeypatch, a, b)
        monkeypatch.setattr(C, '_claim_wake', AsyncMock(return_value='b'))
        try:
            assert await a.workflow.wake(1)
            a.workflow.endpointed = True
            ev = {'session': 9, 'age_ms': 0, 'score': 0.9, 'threshold': 0.8, 'floor': 0.001, 'arrived': 0.0}
            assert await b.workflow_wake(ev)
            assert b.workflow.awake and not a.workflow.awake
        finally:
            await a.workflow.stop()
            await b.workflow.stop()
    asyncio.run(run())


def test_the_settings_view_lists_who_shares_and_who_gets_the_words(fresh_db, monkeypatch):
    import em_routes_workflow as R
    a, b, c = configured('a'), configured('b'), configured('c', 'Kitchen')
    b.workflow.ws.echomuse_peer_context = False
    fleet(monkeypatch, a, b, c)
    view = R._conversation_view('a', a.workflow.config)
    assert view == [{'device_id': 'b', 'label': 'b', 'context': False}]
    assert R._conversation_view('c', c.workflow.config) == []
    assert R._conversation_view('a', {'mode': W.PIPECAT}) == []
