"""Exercise Pipecat's real disconnect/result scan without cloud or tool access."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import echomuse


def service():
    # Use real context messages and upstream lifecycle methods. Only network,
    # pipeline metrics and task cleanup are replaced; no real tools are called.
    return SimpleNamespace(
        _context=LLMContext([
            {'role': 'tool', 'tool_call_id': 'old', 'content': '{"ok":true}'},
            {'role': 'tool', 'tool_call_id': 'busy', 'content': 'IN_PROGRESS'},
        ]),
        _completed_tool_calls={'old'},
        _tool_call_id_to_name={'old': 'EchoMuseList'},
        _supports_non_blocking_tools=False,
        _async_tool_warning_logged=False,
        _connection_task=None,
        _transcription_timeout_task=None,
        _session=SimpleNamespace(close=AsyncMock()),
        _cancel_end_frame_deferral_timeout=lambda: None,
        stop_all_metrics=AsyncMock(),
        push_error=AsyncMock(),
        _tool_result=AsyncMock(),
    )


@pytest.mark.parametrize('patched', [False, True])
def test_reconnect_replay_and_pending_results(patched):
    async def scenario():
        live = service()
        await GeminiLiveLLMService._disconnect(live)
        assert live._completed_tool_calls == set()
        assert len(live._context.get_messages()) == 2

        async def connected(config):
            # A new user request completes after the connection is ready.
            live._context.add_message({
                'role': 'tool', 'tool_call_id': 'stop', 'content': '{"stopped":true}',
            })
            live._tool_call_id_to_name['stop'] = 'EchoMuseStop'
            await GeminiLiveLLMService._process_completed_function_calls(live, True)

        if patched:
            await echomuse.keep_connection(live, connected, None)
        else:
            await connected(None)
        ids = [call.args[0] for call in live._tool_result.await_args_list]
        assert ids == (['stop'] if patched else ['old', 'stop'])

        # A call still pending at reconnect must deliver normally when done.
        live._context.get_messages()[1]['content'] = '{"ok":true}'
        await GeminiLiveLLMService._process_completed_function_calls(live, True)
        assert live._tool_result.await_args_list[-1].args[0] == 'busy'
        count = live._tool_result.await_count
        await GeminiLiveLLMService._process_completed_function_calls(live, True)
        assert live._tool_result.await_count == count

        if patched:
            # Another renewal must not replay either old or newly finished calls.
            await GeminiLiveLLMService._disconnect(live)
            async def reconnect(config):
                await GeminiLiveLLMService._process_completed_function_calls(live, True)
            await echomuse.keep_connection(live, reconnect, None)
            assert live._tool_result.await_count == count
        live.push_error.assert_not_awaited()

    asyncio.run(scenario())
