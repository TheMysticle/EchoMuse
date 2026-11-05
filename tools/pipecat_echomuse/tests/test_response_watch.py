"""Silence deadline and Gemini cancellation, without model/network calls."""
import asyncio
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
import package


def service():
    return SimpleNamespace(
        _em_response_watch_enabled=True,
        _session=SimpleNamespace(send_realtime_input=AsyncMock()),
        _vad_disabled=True, _ready_for_realtime_input=True,
        _user_is_speaking=False, _flush_user_audio_preroll=AsyncMock(),
        push_frame=AsyncMock(), _disconnect=AsyncMock(),
        create_task=lambda coro, name: asyncio.create_task(coro, name=name))


def test_silence_deadline_is_not_extended_by_tools_or_duplicate_stop(monkeypatch):
    async def run():
        monkeypatch.setattr(echomuse, 'RESPONSE_START_SECONDS', .01)
        live = service()
        echomuse.reply_user_turn(live)
        echomuse.watch_response(live)
        task = live._em_response_task
        echomuse.reply_tool_call(live)
        echomuse.reply_tool_result(live)
        echomuse.watch_response(live)
        assert live._em_response_task is task
        await task
        assert live._em_turn_expired
        assert live.push_frame.call_args.args[0].message['type'] == 'echomuse-response-timeout'
        live._disconnect.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize('event', ['reply', 'new_speech', 'interrupt', 'disconnect'])
def test_progress_or_cancellation_disarms_the_old_deadline(monkeypatch, event):
    async def run():
        monkeypatch.setattr(echomuse, 'RESPONSE_START_SECONDS', .01)
        live = service()
        echomuse.reply_user_turn(live)
        echomuse.watch_response(live)
        task = live._em_response_task
        if event == 'reply':
            echomuse.reply_output_transcription(live)
        elif event == 'new_speech':
            echomuse.reply_user_turn(live)
        elif event == 'interrupt':
            await echomuse.interrupt_live_response(live)
        else:
            echomuse.cancel_response_watch(live)
        await asyncio.gather(task, return_exceptions=True)
        live.push_frame.assert_not_awaited()
        live._disconnect.assert_not_awaited()
    asyncio.run(run())


def test_interrupt_reaches_gemini_and_next_speech_reuses_activity_window():
    async def run():
        live = service()
        await echomuse.interrupt_live_response(live)
        await echomuse.interrupt_live_response(live)
        live._session.send_realtime_input.assert_awaited_once()
        assert 'activity_start' in live._session.send_realtime_input.call_args.kwargs
        assert live._em_drop_response
        start = AsyncMock()
        await echomuse.start_live_activity(live, object(), start)
        start.assert_not_awaited()
        live._flush_user_audio_preroll.assert_awaited_once()
        assert live._user_is_speaking and not live._em_interrupt_activity
        await echomuse.start_live_activity(live, object(), start)
        start.assert_awaited_once()
    asyncio.run(run())


def test_interrupt_during_speech_does_not_start_a_second_activity():
    async def run():
        live = service()
        live._user_is_speaking = True
        await echomuse.interrupt_live_response(live)
        live._session.send_realtime_input.assert_not_awaited()
    asyncio.run(run())


UPSTREAM = Path.home() / 'pipecat-upstream/addons/pipecat_assist/app'


@pytest.mark.skipif(not UPSTREAM.exists(), reason='pinned upstream checkout not present')
def test_packaged_protocol_emits_turn_markers_and_fatal_timeout(tmp_path):
    path = tmp_path / 'protocol.py'
    path.write_text(package.adapt_protocol((UPSTREAM / 'va_pipecat_protocol.py').read_text()))
    spec = importlib.util.spec_from_file_location('response_watch_protocol', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    protocol = module.VaPipecatProtocol(lambda *a: False)
    def event(kind):
        return json.loads(protocol.on_rtvi_message({'label': 'rtvi-ai', 'type': kind}))
    assert event('user-started-speaking')['user_started']
    assert event('user-stopped-speaking')['user_stopped']
    assert event('echomuse-response-timeout')['recoverable'] is False
    source = package.adapt((UPSTREAM / 'main.py').read_text())
    compile(source, '<adapted>', 'exec')
    for hook in ('watch_response(self)', 'interrupt_live_response(self)', 'cancel_response_watch(self)'):
        assert 'echomuse.' + hook in source


@pytest.mark.skipif(not UPSTREAM.exists(), reason='pinned upstream checkout not present')
def test_generated_service_interrupt_and_next_turn_use_real_gemini_activity_methods():
    import ast
    from loguru import logger
    from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
    import heard_check

    class Base:
        _handle_user_started_speaking = GeminiLiveLLMService._handle_user_started_speaking
        _handle_user_stopped_speaking = GeminiLiveLLMService._handle_user_stopped_speaking
        _handle_interruption = GeminiLiveLLMService._handle_interruption
        async def _handle_msg_model_turn(self, message):
            self.delivered += 1
        async def _handle_msg_tool_call(self, message):
            self.called += 1

    source = package.adapt((UPSTREAM / 'main.py').read_text())
    cls = next(n for n in ast.walk(ast.parse(source))
               if isinstance(n, ast.ClassDef) and n.name == 'ResilientGeminiLiveLLMService')
    namespace = {'GeminiLiveLLMService': Base, 'echomuse': echomuse,
                 'heard_check': heard_check, 'logger': logger}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), '<packaged service>', 'exec'), namespace)

    async def run():
        live = namespace[cls.name]()
        live.__dict__.update(service().__dict__)
        live._bot_is_responding = False
        live._needs_initial_turn_complete_message = False
        live.start_ttfb_metrics = AsyncMock()
        live.delivered = live.called = 0
        message = SimpleNamespace(server_content=SimpleNamespace(model_turn=SimpleNamespace(
            parts=[SimpleNamespace(text='Done.', thought=False, inline_data=None)])))
        await live._handle_interruption()
        await live._handle_msg_model_turn(message)  # old reply still in flight
        await live._handle_msg_tool_call(None)
        assert live.delivered == live.called == 0
        await live._handle_user_started_speaking(None)
        await live._handle_user_stopped_speaking(None)
        assert [list(c.kwargs) for c in live._session.send_realtime_input.call_args_list] == [
            ['activity_start'], ['activity_end']]
        task = live._em_response_task
        await live._handle_msg_model_turn(message)
        assert live.delivered == 1
        await asyncio.gather(task, return_exceptions=True)
        live._em_turn_expired = True
        await live._handle_msg_tool_call(None)
        assert live.called == 0
    asyncio.run(run())
