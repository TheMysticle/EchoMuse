"""Echoes that share a conversation: the other Echo's exchange as a note.

The note must never cost a wake anything: it is sent only while the Echo is
idle (Gemini 3.1 Live closes the session on text inside an activity window,
probed 2026-11-02), its answer is dropped, and a wake simply interrupts it.
"""
import asyncio
import base64
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import echomuse


def b64(text):
    return base64.b64encode(text.encode()).decode()


def message(user='Która godzina?', reply='Jest piętnasta.'):
    return json.dumps({'type': 'peer_context', 'user_b64': b64(user), 'reply_b64': b64(reply)})


def service(**state):
    live = SimpleNamespace(
        _session=SimpleNamespace(send_realtime_input=AsyncMock()),
        _disconnecting=False,
        _ready_for_realtime_input=True,
        _needs_initial_turn_complete_message=False,
        _user_is_speaking=False,
        _bot_is_responding=False,
        _em_response_task=None,
    )
    for key, value in state.items():
        setattr(live, key, value)
    return live


def sent(live):
    return [call.kwargs for call in live._session.send_realtime_input.await_args_list]


def test_a_peer_message_parses_to_its_two_texts():
    assert echomuse.parse_peer_context(message()) == ('Która godzina?', 'Jest piętnasta.')


@pytest.mark.parametrize('data', [
    '{"type": "wake"}', '{"type": "interrupt"}', 'not json', '[]', b'\x00\x01',
    json.dumps({'type': 'peer_context', 'user_b64': '%%%', 'reply_b64': ''}),
    json.dumps({'type': 'peer_context', 'user_b64': 5, 'reply_b64': ''}),
    json.dumps({'type': 'peer_context', 'user_b64': '', 'reply_b64': ''}),
    json.dumps({'type': 'peer_context', 'user_b64': b64('x' * 5000), 'reply_b64': ''}),
])
def test_anything_else_is_not_a_peer_message(data):
    assert echomuse.parse_peer_context(data) is None


def test_texts_are_bounded_and_whitespace_collapsed():
    user, reply = echomuse.parse_peer_context(message('a\n\n b ' + 'u' * 600, 'r' * 1200))
    assert user.startswith('a b ') and len(user) == echomuse.PEER_USER_MAX
    assert len(reply) == echomuse.PEER_REPLY_MAX


def test_an_idle_echo_gets_the_note_and_its_answer_is_dropped():
    live = service()
    echomuse.note_peer_context(live, 'What is on the shopping list?', 'Milk and eggs.')
    assert asyncio.run(echomuse.flush_peer_context(live))
    [call] = sent(live)
    assert set(call) == {'text'}
    assert '"What is on the shopping list?"' in call['text'] and '"Milk and eggs."' in call['text']
    assert live._em_drop_response is True
    assert live._em_peer_notes == []


@pytest.mark.parametrize('state', [
    {'_user_is_speaking': True}, {'_bot_is_responding': True}, {'_em_response_task': object()},
    {'_session': None}, {'_disconnecting': True}, {'_ready_for_realtime_input': False},
    {'_needs_initial_turn_complete_message': True}])
def test_nothing_is_sent_unless_idle_and_the_note_waits(state):
    live = service(**state)
    session = live._session
    echomuse.note_peer_context(live, 'Turn on the lamp', 'Done.')
    assert not asyncio.run(echomuse.flush_peer_context(live))
    if session is not None:
        assert session.send_realtime_input.await_count == 0
    assert len(live._em_peer_notes) == 1
    assert not getattr(live, '_em_drop_response', False)


def test_a_reply_still_awaited_or_nudged_holds_the_note():
    live = service()
    echomuse.reply_user_turn(live)
    echomuse.reply_tool_result(live)
    echomuse.note_peer_context(live, 'x', 'y')
    assert not asyncio.run(echomuse.flush_peer_context(live))
    watch = echomuse._reply_watch(live)
    watch.pending, watch.nudged = False, True
    assert not asyncio.run(echomuse.flush_peer_context(live))
    watch.heard = True
    assert asyncio.run(echomuse.flush_peer_context(live))


def test_an_open_interrupt_window_is_closed_before_the_text():
    from google.genai.types import ActivityEnd
    live = service(_em_interrupt_activity=True)
    echomuse.note_peer_context(live, 'x', 'y')
    asyncio.run(echomuse.flush_peer_context(live))
    calls = sent(live)
    assert list(calls[0]) == ['activity_end'] and isinstance(calls[0]['activity_end'], ActivityEnd)
    assert list(calls[1]) == ['text']
    assert live._em_interrupt_activity is False


def test_only_the_last_few_recent_exchanges_are_kept():
    live = service()
    echomuse.note_peer_context(live, 'old', '', now=0)
    for i in range(5):
        echomuse.note_peer_context(live, f'u{i}', '', now=1000 + i)
    assert [n[1] for n in live._em_peer_notes] == ['u2', 'u3', 'u4']
    live2 = service()
    echomuse.note_peer_context(live2, 'stale', '', now=0)
    assert not asyncio.run(echomuse.flush_peer_context(live2, now=echomuse.PEER_NOTE_MAX_AGE_S + 1))
    assert sent(live2) == []


def test_a_send_failure_is_logged_never_raised():
    live = service()
    live._session.send_realtime_input.side_effect = RuntimeError('closed')
    echomuse.note_peer_context(live, 'x', 'y')
    assert asyncio.run(echomuse.flush_peer_context(live)) is False


def test_the_frame_is_a_system_frame_recognised_by_the_service_hook():
    from pipecat.frames.frames import SystemFrame
    frame = echomuse.peer_context_frame(message())
    assert isinstance(frame, SystemFrame) and echomuse.is_peer_context(frame)
    assert (frame.user, frame.reply) == ('Która godzina?', 'Jest piętnasta.')
    assert echomuse.peer_context_frame('{"type": "wake"}') is None


UPSTREAM = Path.home()/'pipecat-upstream/addons/pipecat_assist'


def _packager():
    spec = importlib.util.spec_from_file_location('peer_packager', ROOT/'package.py')
    packager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packager)
    return packager


@pytest.mark.skipif(not UPSTREAM.exists(), reason='pinned upstream checkout not present')
def test_packaged_service_and_transport_are_wired():
    packager = _packager()
    main = packager.adapt((UPSTREAM/'app/main.py').read_text(encoding='utf-8'))
    start = main.index('class ResilientGeminiLiveLLMService(')
    body = main[start:main.index('settings_kwargs: dict[str, Any]', start)]
    assert body.count('async def process_frame(') == 1
    assert body.count('await echomuse.take_peer_context(self, frame)') == 1
    assert body.count('await echomuse.flush_peer_context(self)') == 1
    # The flag is offered only where the note can be delivered (Gemini Live).
    assert '"peer_context": _runtime_mode(flow) != "composed" and' in main
    va = packager.adapt_va_pipecat((UPSTREAM/'app/va_pipecat.py').read_text(encoding='utf-8'))
    hook = va.index('peer = echomuse.peer_context_frame(data)')
    assert hook < va.index('action = self.protocol.client_action(data)')
    assert 'from app import echomuse\n' in va


BUILT = ROOT.parents[1]/'dist/pipecat-assist-echomuse-dev'


@pytest.mark.skipif(not (BUILT/'app/va_pipecat.py').exists(), reason='add-on not built')
def test_the_built_serializer_turns_the_message_into_the_frame_and_nothing_else_changes():
    sys.path.insert(0, str(BUILT))
    try:
        for name in [m for m in sys.modules if m == 'app' or m.startswith('app.')]:
            del sys.modules[name]
        from app import va_pipecat
        from app import echomuse as built_echomuse
        serializer = next(cls for cls in vars(va_pipecat).values()
                          if isinstance(cls, type) and hasattr(cls, 'deserialize') and cls.__module__ == va_pipecat.__name__)
        params = serializer.__init__.__code__.co_varnames[1:serializer.__init__.__code__.co_argcount]
        instance = serializer(*[(lambda *a, **k: False) for _ in params])

        async def go():
            frame = await instance.deserialize(message())
            wake = await instance.deserialize('{"type": "wake"}')
            interrupt = await instance.deserialize('{"type": "interrupt"}')
            return frame, wake, interrupt
        frame, wake, interrupt = asyncio.run(go())
        assert built_echomuse.is_peer_context(frame)
        assert wake is None and type(interrupt).__name__ == 'InterruptionWorkerFrame'
    finally:
        sys.path.remove(str(BUILT))
        for name in [m for m in sys.modules if m == 'app' or m.startswith('app.')]:
            del sys.modules[name]


def test_a_burst_of_exchanges_goes_out_as_one_note():
    live = service()

    async def go():
        for user in ('first', 'second', 'third'):
            await echomuse.take_peer_context(live, SimpleNamespace(user=user, reply='ok'), delay=0.05)
        await live._em_peer_flush_task
    asyncio.run(go())
    [call] = sent(live)
    assert all(f'"{u}"' in call['text'] for u in ('first', 'second', 'third'))
