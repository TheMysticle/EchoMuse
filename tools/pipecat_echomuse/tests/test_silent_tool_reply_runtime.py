"""Gemini Live ending a turn after a tool result without speaking.

The message order is the one in the 2026-11-02 field log: in the four silent
turns turn_complete followed the tool result with no model output; in the 54
turns that spoke, model output came first. Messages are real google.genai
types, so a renamed field fails here rather than on an Echo.
"""
import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import echomuse


def service(vad_disabled=True):
    return SimpleNamespace(
        _session=SimpleNamespace(send_realtime_input=AsyncMock()),
        _disconnecting=False,
        _user_is_speaking=False,
        _vad_disabled=vad_disabled,
        start_ttfb_metrics=AsyncMock(),
        _handle_send_error=AsyncMock(),
    )


def turn(*parts):
    return types.LiveServerMessage(server_content=types.LiveServerContent(
        model_turn=types.Content(role='model', parts=list(parts))))


AUDIO = types.Part(inline_data=types.Blob(data=b'\x00\x01', mime_type='audio/pcm;rate=24000'))
TEXT = types.Part(text='Done.')
THOUGHT = types.Part(text='The user wants the AC at 19.5.', thought=True)


def sent(live, session=None):
    session = session or live._session
    return [call.kwargs for call in session.send_realtime_input.await_args_list]


def run(live, *steps):
    async def go():
        for step in steps:
            if step == 'user':
                echomuse.reply_user_turn(live)
            elif step == 'call':
                echomuse.reply_tool_call(live)
            elif step == 'result':
                echomuse.reply_tool_result(live)
            elif step == 'transcript':
                echomuse.reply_output_transcription(live)
            elif step == 'complete':
                await echomuse.answer_silent_tool_turn(live)
            else:
                echomuse.reply_model_turn(live, step)
    session = live._session
    asyncio.run(go())
    return sent(live, session) if session else []


NUDGE = [{'text': echomuse.SILENT_TOOL_REPLY_PROMPT}]


@pytest.mark.parametrize('vad_disabled', [True, False])
def test_silent_turn_after_tool_result_is_asked_to_answer_with_bare_text(vad_disabled):
    # Never wrapped in activity_start/activity_end: Gemini 3.1 Live refuses
    # text inside an activity window with 1007 and closes the session.
    live = service(vad_disabled)
    assert run(live, 'user', 'call', 'result', 'complete') == NUDGE
    live.start_ttfb_metrics.assert_awaited_once()


def test_a_turn_of_thoughts_alone_is_still_silent():
    assert len(run(service(), 'user', 'call', 'result', turn(THOUGHT), 'complete')) == 1


@pytest.mark.parametrize('output', [turn(AUDIO), turn(TEXT), 'transcript'])
def test_a_turn_that_spoke_is_left_alone(output):
    assert run(service(), 'user', 'call', 'result', output, 'complete') == []


def test_turn_without_a_tool_is_left_alone():
    assert run(service(), 'user', 'complete') == []


def test_turn_complete_before_the_result_is_left_alone():
    # The tool call's own turn may close before its result is sent.
    assert run(service(), 'user', 'call', 'complete', 'result', turn(AUDIO), 'complete') == []


def test_a_second_tool_call_waits_for_its_own_result():
    live = service()
    assert run(live, 'user', 'call', 'result', 'call', 'complete') == []
    assert len(run(live, 'result', 'complete')) == 1


def test_asked_once_per_user_turn_then_again_on_the_next():
    live = service()
    run(live, 'user', 'call', 'result', 'complete', 'call', 'result', 'complete')
    assert len(sent(live)) == 1
    run(live, 'user', 'call', 'result', 'complete')
    assert len(sent(live)) == 2


@pytest.mark.parametrize('state', [
    {'_user_is_speaking': True}, {'_disconnecting': True}, {'_session': None}])
def test_never_sent_over_the_user_or_without_a_session(state):
    live = service()
    session = live._session
    for key, value in state.items():
        setattr(live, key, value)
    run(live, 'user', 'call', 'result', 'complete')
    assert session.send_realtime_input.await_count == 0


def test_send_failure_goes_to_pipecats_handler():
    live = service()
    error = RuntimeError('closed')
    live._session.send_realtime_input.side_effect = error
    run(live, 'user', 'call', 'result', 'complete')
    live._handle_send_error.assert_awaited_once_with(error)


UPSTREAM = Path.home()/'pipecat-upstream/addons/pipecat_assist'


@pytest.mark.skipif(not UPSTREAM.exists(), reason='pinned upstream checkout not present')
def test_packaged_service_wires_every_hook():
    spec = importlib.util.spec_from_file_location('silent_packager', ROOT/'package.py')
    packager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packager)
    main = packager.adapt((UPSTREAM/'app/main.py').read_text(encoding='utf-8'))
    start = main.index('class ResilientGeminiLiveLLMService(')
    body = main[start:main.index('settings_kwargs: dict[str, Any]', start)]
    for hook in ('echomuse.reply_user_turn(self)', 'echomuse.reply_tool_result(self)',
                 'echomuse.reply_tool_call(self)', 'echomuse.reply_model_turn(self, message)',
                 'echomuse.reply_output_transcription(self)',
                 'await echomuse.answer_silent_tool_turn(self)'):
        assert body.count(hook) == 1, hook
    # One definition each: a second, later def would silently replace the hook.
    for name in ('_handle_user_started_speaking', '_tool_result', '_handle_msg_tool_call',
                 '_handle_msg_turn_complete', '_handle_msg_model_turn'):
        assert body.count(f'async def {name}(') == 1, name


@pytest.mark.parametrize('spoken', [turn(AUDIO), 'transcript'])
def test_a_turn_that_spoke_before_its_tool_call_is_not_silent(spoken):
    # "Set a timer" -> "For how long?" + EchoMuseRequestContinuation, then the
    # tool result answered with turn_complete alone: the question was heard.
    assert run(service(), 'user', spoken, 'call', 'result', 'complete') == []


def test_what_was_heard_in_one_turn_does_not_excuse_the_next():
    live = service()
    run(live, 'user', turn(AUDIO), 'call', 'result', 'complete')
    assert run(live, 'user', 'call', 'result', 'complete') == NUDGE
