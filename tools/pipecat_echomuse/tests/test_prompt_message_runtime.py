"""A controller prompt: the model speaks first (em_workflow.Bridge.prompt).

Sent like a peer note, only while the model is idle and outside any activity
window (Gemini closes the session on text inside one), but its answer is the
point: it is never dropped.
"""
import asyncio
import base64
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import echomuse


def message(text):
    return json.dumps({'type': 'prompt', 'text_b64': base64.b64encode(text.encode()).decode()})


def service(**state):
    live = SimpleNamespace(
        _session=SimpleNamespace(send_realtime_input=AsyncMock()),
        _disconnecting=False, _ready_for_realtime_input=True,
        _needs_initial_turn_complete_message=False, _user_is_speaking=False,
        _bot_is_responding=False, _em_response_task=None, _em_drop_response=True,
    )
    for key, value in state.items():
        setattr(live, key, value)
    return live


def test_a_prompt_parses_to_its_text():
    assert echomuse.parse_prompt(message('  Ask:\n which   colour? ')) == 'Ask: which colour?'


@pytest.mark.parametrize('data', [
    '{"type": "wake"}', 'not json', b'\x00', '[]',
    json.dumps({'type': 'prompt', 'text_b64': '%%%'}),
    json.dumps({'type': 'prompt', 'text_b64': 5}),
    json.dumps({'type': 'prompt', 'text_b64': ''}),
    json.dumps({'type': 'prompt', 'text_b64': base64.b64encode(b'x' * 20000).decode()}),
])
def test_anything_else_is_not_a_prompt(data):
    assert echomuse.parse_prompt(data) is None


def test_an_idle_model_is_asked_at_once_and_its_answer_is_kept():
    async def main():
        live = service()
        assert await echomuse.flush_prompt(live) is False          # nothing waiting
        live._em_prompt = (0.0, 'Say hello')
        assert await echomuse.flush_prompt(live, now=1.0)
        text = live._session.send_realtime_input.await_args.kwargs['text']
        assert text.endswith('Say hello') and 'Not from the user' in text
        assert live._em_drop_response is False
        assert live._em_prompt is None
    asyncio.run(main())


def test_a_busy_model_keeps_it_for_the_next_turn_complete_and_a_stale_one_is_dropped():
    async def main():
        live = service(_bot_is_responding=True)
        live._em_prompt = (0.0, 'Say hello')
        assert not await echomuse.flush_prompt(live, now=1.0)
        assert live._em_prompt is not None
        live._bot_is_responding = False
        assert not await echomuse.flush_prompt(live, now=echomuse.PROMPT_WAIT_S + 1)
        assert live._em_prompt is None
        live._session.send_realtime_input.assert_not_awaited()
    asyncio.run(main())


def test_the_scheduler_allowlist_admits_the_claude_tools():
    _, allowed = echomuse.mcp_options({'scheduler': {'id': 'echomuse'}}, [], ['HassTurnOn'])
    assert {'ClaudeStart', 'ClaudeStatus', 'ClaudeReply', 'ClaudePrint', 'ClaudeCancel'} <= set(allowed)
    _, allowed = echomuse.mcp_options({}, [], ['HassTurnOn'])
    assert 'ClaudeStart' not in allowed
