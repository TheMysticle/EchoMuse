"""Claude Code on a PC, by voice (em_claude_bridge, docs/claude-bridge.md).

A Pipecat Echo gets four tools on its own satellite MCP endpoint, which the
controller forwards to the PC bridge with that Echo's id; the bridge's
questions and results come back as a session the controller opens on that
Echo (Bridge.prompt): the model speaks first, and only then is the Echo's
microphone opened, as the bounded turn stream a follow-up uses.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import em_claude_bridge as CB
import em_player
import em_workflow as W
import em_workflow_tools
from test_voice_workflow import device
import test_voice_workflow as workflow_fixtures

fresh_db = workflow_fixtures.fresh_db


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    device()
    import em_controller as C
    monkeypatch.setattr(C, '_push_device_state', AsyncMock())
    monkeypatch.setattr(em_player, '_sessions', {})


def bridge(prompt=True):
    d = device()
    d.workflow.config = {'mode': W.PIPECAT, 'endpoint': 'ws://unused/api/assist/esphome'}
    d.workflow.status = 'connected'
    d.workflow.ws = SimpleNamespace(send=AsyncMock(), close=AsyncMock(), echomuse_prompt=prompt,
                                    echomuse_turn_events=True)
    d.data_ws.send.side_effect = None
    d.mic_start_turn = AsyncMock()
    d.mic_start = AsyncMock()
    d.mic_stop = AsyncMock()
    return d


def sent_prompt(d):
    for call in d.workflow.ws.send.await_args_list:
        message = json.loads(call.args[0])
        if message.get('type') == 'prompt':
            import base64
            return base64.b64decode(message['text_b64']).decode()
    return None


def test_a_prompt_opens_a_session_with_the_microphone_closed(fresh_db):
    async def main():
        d = bridge()
        try:
            assert await d.workflow.prompt('Ask: English or Polish?') is None
            assert sent_prompt(d) == 'Ask: English or Polish?'
            assert d.workflow.awake and d.workflow.prompting
            assert d.workflow.uplink_closed and not d.workflow.accept_audio
            assert not d.workflow.awaiting_user_turn          # the reply is the first thing said
            d.mic_start_turn.assert_not_awaited()
            d.mic_start.assert_not_awaited()
            assert not d.workflow.capturing                   # no wake on another Echo gives way to it
            assert await d.workflow.prompt('again') == 'busy'
        finally:
            await d.workflow.stop()
    asyncio.run(main())


def test_after_the_reply_the_turn_stream_opens_for_the_answer(fresh_db):
    async def main():
        d = bridge()
        try:
            await d.workflow.prompt('Ask something')
            await d.workflow.on_event({'type': 'phase', 'phase': 'listening', 'follow_up': True})
            assert d.workflow.awake and not d.workflow.prompting
            d.mic_start_turn.assert_awaited_once()
            # The answer's reply ends the session as an ordinary reply does.
            await d.workflow.on_event({'type': 'phase', 'phase': 'listening', 'follow_up': True})
            assert not d.workflow.awake
        finally:
            await d.workflow.stop()
    asyncio.run(main())


@pytest.mark.parametrize('setup, why', [
    (lambda d: setattr(d.workflow.ws, 'echomuse_prompt', False), 'adapter_required'),
    (lambda d: setattr(d, 'muted', True), 'busy'),
    (lambda d: setattr(d, 'wake_word_enabled', False), 'busy'),
    (lambda d: setattr(d.workflow, 'status', 'reconnecting'), 'unavailable'),
    (lambda d: setattr(d.workflow, 'config', {'mode': W.NATIVE}), 'unavailable'),
])
def test_a_prompt_is_refused_where_it_could_not_be_said_or_must_not_listen(fresh_db, setup, why):
    async def main():
        d = bridge()
        setup(d)
        try:
            assert await d.workflow.prompt('hello') == why
            assert not d.workflow.awake and sent_prompt(d) is None
        finally:
            await d.workflow.stop()
    asyncio.run(main())


def test_a_wake_during_a_prompt_takes_the_session_over(fresh_db):
    async def main():
        d = bridge()
        try:
            await d.workflow.prompt('Ask something')
            assert await d.workflow.wake(7)
            assert d.workflow.session == 7 and not d.workflow.prompting and not d.workflow.uplink_closed
        finally:
            await d.workflow.stop()
    asyncio.run(main())


# ----- tools ---------------------------------------------------------------

class Request(dict):
    def __init__(self, device_id, payload):
        super().__init__(schedule_satellite=device_id)
        self.match_info = {'id': device_id}
        self._payload = payload

    async def json(self):
        return self._payload


def rpc(method, params=None):
    return {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}


def names(response):
    return [t['name'] for t in json.loads(response.text)['result']['tools']]


def test_the_tools_are_listed_only_when_the_bridge_is_configured(monkeypatch, fresh_db):
    async def main():
        d = bridge()
        monkeypatch.delenv('CLAUDE_BRIDGE_URL', raising=False)
        monkeypatch.delenv('CLAUDE_BRIDGE_TOKEN', raising=False)
        listed = names(await em_workflow_tools.handle(Request('satellite', rpc('tools/list')), {'satellite': d}))
        assert not CB.TOOL_NAMES & set(listed)
        monkeypatch.setenv('CLAUDE_BRIDGE_URL', 'http://pc.lan:8790/')
        monkeypatch.setenv('CLAUDE_BRIDGE_TOKEN', 'secret')
        listed = names(await em_workflow_tools.handle(Request('satellite', rpc('tools/list')), {'satellite': d}))
        assert CB.TOOL_NAMES <= set(listed)
    asyncio.run(main())


def test_a_tool_call_carries_the_satellite_of_the_key_never_an_argument(monkeypatch, fresh_db):
    async def main():
        d = bridge()
        monkeypatch.setenv('CLAUDE_BRIDGE_URL', 'http://pc.lan:8790')
        monkeypatch.setenv('CLAUDE_BRIDGE_TOKEN', 'secret')
        d.workflow.config = {'mode': W.PIPECAT, 'endpoint': 'ws://unused'}
        calls = []

        async def fake_call(method, path, body=None, **_):
            calls.append((method, path, body))
            return {'task_id': 'coffee-tamper-1a2b', 'status': 'running'}
        monkeypatch.setattr(CB, '_call', fake_call)
        args = {'request': 'A tamper for Dolce Gusto capsules', 'title': 'Coffee tamper'}
        response = await em_workflow_tools.handle(
            Request('satellite', rpc('tools/call', {'name': 'ClaudeStart', 'arguments': args})), {'satellite': d})
        result = json.loads(response.text)['result']
        assert not result['isError']
        assert calls == [('POST', '/tasks', {'device': 'satellite', **args})]
        bad = await CB.tool('ClaudeStart', {**args, 'device': 'kitchen'}, 'satellite')
        assert bad['isError'] and len(calls) == 1
    asyncio.run(main())


@pytest.mark.parametrize('name, args', [
    ('ClaudeReply', {'task_id': '../events', 'text': 'x'}),
    ('ClaudeStatus', {'task_id': 'A/B'}),
    ('ClaudeCancel', {}),
    ('ClaudeStart', {'request': '', 'title': 'x'}),
    ('ClaudeStart', {'request': 'x' * (CB.TEXT_MAX + 1), 'title': 'x'}),
    ('ClaudeReply', {'task_id': 'ok-1', 'text': 5}),
    ('Nope', {}),
])
def test_arguments_outside_the_schema_are_refused(name, args):
    with pytest.raises(ValueError):
        CB.check_args(name, args)


def test_a_task_id_is_never_a_path():
    assert CB.check_args('ClaudeStatus', {'task_id': 'coffee-tamper-1a2b'}) == {'task_id': 'coffee-tamper-1a2b'}


def test_what_is_said_for_each_event():
    ask = CB.spoken_instruction({'type': 'ask', 'task_id': 't-1', 'title': 'Tamper', 'text': 'Measure it?'})
    assert 'Measure it?' in ask and 'ClaudeReply' in ask and '"t-1"' in ask
    done = CB.spoken_instruction({'type': 'done', 'task_id': 't-1', 'title': 'Tamper', 'text': 'Made it.'})
    assert 'Made it.' in done and 'finished' in done
    assert 'problem' in CB.spoken_instruction({'type': 'failed', 'task_id': 't-1', 'text': 'x'})
    assert CB.spoken_instruction({'type': 'clear', 'task_id': 't-1'}) is None
    assert CB.spoken_instruction({'type': 'ask', 'task_id': 't-1', 'text': ''}) is None


def test_a_busy_echo_is_retried_and_a_missing_adapter_is_not():
    async def main():
        answers = ['busy', 'busy', None]
        workflow = SimpleNamespace(prompt=AsyncMock(side_effect=lambda text, listen=True: answers.pop(0)))
        devices = {'kitchen': SimpleNamespace(workflow=workflow)}
        event = {'type': 'ask', 'task_id': 't-1', 'device': 'kitchen', 'text': 'Which colour?'}
        assert await CB.deliver(event, devices, retry_s=0, give_up_s=5)
        assert workflow.prompt.await_count == 3
        workflow.prompt = AsyncMock(return_value='adapter_required')
        assert not await CB.deliver(event, devices, retry_s=0, give_up_s=5)
        assert workflow.prompt.await_count == 1
        workflow.prompt = AsyncMock(return_value='busy')
        assert not await CB.deliver(event, devices, retry_s=0.01, give_up_s=0.03)
    asyncio.run(main())


def test_a_newer_event_replaces_the_delivery_still_waiting(monkeypatch):
    async def main():
        started = []

        async def slow(event, devices):
            started.append(event['type'])
            await asyncio.sleep(10)
        monkeypatch.setattr(CB, 'deliver', slow)
        CB._schedule({'type': 'ask', 'task_id': 't-1'}, {})
        await asyncio.sleep(0)
        first = CB._deliveries['t-1']
        CB._schedule({'type': 'clear', 'task_id': 't-1'}, {})
        await asyncio.sleep(0)
        assert first.cancelled() or first.cancelling()
        CB._deliveries.pop('t-1').cancel()
    asyncio.run(main())


# ----- the printer --------------------------------------------------------

def test_a_print_request_goes_to_the_bridge_and_nothing_else_does(monkeypatch):
    async def main():
        monkeypatch.setenv('CLAUDE_BRIDGE_URL', 'http://pc.lan:8790')
        monkeypatch.setenv('CLAUDE_BRIDGE_TOKEN', 'secret')
        calls = []

        async def fake_call(method, path, body=None, **kw):
            calls.append((method, path, body))
            return {'started': True}
        monkeypatch.setattr(CB, '_call', fake_call)
        assert not (await CB.tool('ClaudePrint', {'action': 'start'}, 'kitchen'))['isError']
        assert not (await CB.tool('ClaudePrint', {'action': 'stop', 'task_id': 'tamper-1a2b'}, 'kitchen'))['isError']
        assert calls == [('POST', '/printer', {'action': 'start'}),
                         ('POST', '/printer', {'action': 'stop', 'task_id': 'tamper-1a2b'})]
        assert (await CB.tool('ClaudePrint', {'action': 'delete_all'}, 'kitchen'))['isError']
        assert (await CB.tool('ClaudePrint', {'action': 'start', 'task_id': '../x'}, 'kitchen'))['isError']
        assert len(calls) == 2
    asyncio.run(main())


def test_a_waiting_print_is_offered_never_started_and_reports_do_not_listen():
    done = CB.spoken_instruction({'type': 'done', 'task_id': 't-1', 'title': 'Tamper', 'text': 'Made it.',
                                  'print': {'minutes': 66, 'material': 'PETG'}})
    assert 'not started' in done and 'about 66 minutes' in done and 'PETG' in done
    assert 'start the print' in done and 'clearly say' in done
    assert CB.listens({'type': 'done'}) and CB.listens({'type': 'ask'})
    printed = CB.spoken_instruction({'type': 'printed', 'task_id': 't-1', 'title': 'Tamper'})
    assert 'finished' in printed and 'Call no tool' in printed and not CB.listens({'type': 'printed'})
    failed = CB.spoken_instruction({'type': 'print_failed', 'task_id': 't-1', 'title': 'Tamper',
                                    'text': 'The printer reported an error.'})
    assert 'reported an error' in failed and not CB.listens({'type': 'print_failed'})


def test_a_report_session_ends_after_it_is_said(fresh_db):
    async def main():
        d = bridge()
        try:
            assert await d.workflow.prompt('The print finished.', listen=False) is None
            await d.workflow.on_event({'type': 'phase', 'phase': 'listening', 'follow_up': True})
            d.mic_start_turn.assert_not_awaited()
            assert not d.workflow.awake
        finally:
            await d.workflow.stop()
    asyncio.run(main())
