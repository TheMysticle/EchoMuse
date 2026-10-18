"""Local HA agent routing, no-match fallback, and at-most-once actions."""
import asyncio
import ast
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(filename, name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'tools/pipecat_echomuse'/filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


commands = load('local_commands.py', 'test_local_command_client')


def payload(text='Light off.', response_type='action_done', code=None, continuing=False):
    return {'response': {'response_type': response_type, 'data': {'code': code},
                         'speech': {'plain': {'speech': text}}},
            'continue_conversation': continuing, 'conversation_id': 'local-conversation'}


def mock_api(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(commands.httpx, 'AsyncClient',
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))


@pytest.mark.parametrize('sentence,speech', [
    ('turn off the kitchen lights', 'Turned off the lights.'),
    ('activate movie mode', 'Movie mode activated.'),
    ('what is the bedroom temperature', 'The temperature is 21 degrees.'),
    ('remind me to take the bins out tomorrow', 'Reminder saved.'),
])
def test_native_agent_handles_builtin_queries_and_custom_commands(monkeypatch, sentence, speech):
    seen = []
    def handle(request):
        seen.append(json.loads(request.content))
        assert request.headers['Authorization'] == 'Bearer test-token'
        assert request.url.path == '/api/conversation/process'
        return httpx.Response(200, json=payload(speech))
    mock_api(monkeypatch, handle)
    client = commands.HomeAssistantCommands('http://ha/api', 'test-token', language='en', device_id='ha-device')
    reply = asyncio.run(client.process(sentence))
    assert reply.text == speech
    assert seen == [{'text': sentence, 'language': 'en', 'agent_id': 'homeassistant', 'device_id': 'ha-device'}]


def test_only_no_match_falls_through_and_handler_errors_keep_ha_speech(monkeypatch):
    results = [payload('No match', 'error', 'no_intent_match'),
               payload('Two lights have that name.', 'error', 'no_valid_targets'),
               payload('The command failed.', 'error', 'failed_to_handle')]
    mock_api(monkeypatch, lambda request: httpx.Response(200, json=results.pop(0)))
    client = commands.HomeAssistantCommands('http://ha/api', 'token', language='en')
    assert asyncio.run(client.process('tell me a joke')) is None
    assert asyncio.run(client.process('turn off the lamp')).text == 'Two lights have that name.'
    assert asyncio.run(client.process('turn off the lamp')).text == 'The command failed.'


def test_follow_up_keeps_conversation_id_only_until_finished(monkeypatch):
    seen = []
    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=payload('Which room?', continuing=len(seen) == 1))
    mock_api(monkeypatch, handle)
    client = commands.HomeAssistantCommands('http://ha/api', 'token', language='en')
    assert asyncio.run(client.process('turn it off')).continue_conversation
    assert not asyncio.run(client.process('kitchen')).continue_conversation
    assert seen[1]['conversation_id'] == 'local-conversation'
    assert client.conversation_id is None


@pytest.mark.parametrize('failure', ['timeout', 'disconnect', 'server_error', 'invalid_json'])
def test_uncertain_action_result_is_terminal_never_an_llm_retry(monkeypatch, failure):
    def handle(request):
        if failure == 'timeout':
            raise httpx.ReadTimeout('timeout')
        if failure == 'disconnect':
            raise httpx.ReadError('connection lost')
        if failure == 'server_error':
            return httpx.Response(500)
        return httpx.Response(200, text='not JSON')
    mock_api(monkeypatch, handle)
    client = commands.HomeAssistantCommands('http://ha/api', 'token', language='en')
    assert asyncio.run(client.process('toggle the light')) is not None


def test_uses_supervisor_or_core_mcp_credentials_only():
    config = SimpleNamespace(effective_mcp_url='https://ha/api/mcp', effective_mcp_token='ha-token')
    client = commands.from_config(config, language='pl', device_id='speaker', environ={})
    assert client.url == 'https://ha/api/conversation/process'
    assert client.device_id == 'speaker' and client.language == 'pl'
    config.effective_mcp_url = 'http://third-party-mcp/sse'
    assert commands.from_config(config, language='en', environ={}) is None
    client = commands.from_config(config, language='en', environ={'SUPERVISOR_TOKEN': 'supervisor-token'})
    assert client.url == 'http://supervisor/core/api/conversation/process'


def test_satellite_identity_is_resolved_from_registry_mac_not_session_id(monkeypatch):
    from websockets.asyncio.server import serve
    real_connect = commands.connect
    seen = []
    async def run():
        async def registry(ws):
            await ws.send(json.dumps({'type': 'auth_required'}))
            auth = json.loads(await ws.recv())
            assert auth == {'type': 'auth', 'access_token': 'test-token'}
            await ws.send(json.dumps({'type': 'auth_ok'}))
            seen.append(json.loads(await ws.recv()))
            await ws.send(json.dumps({'id': 1, 'success': True, 'result': [
                {'id': 'another-speaker', 'connections': [['mac', 'aa:bb:cc:dd:ee:00']], 'identifiers': []},
                {'id': 'real-ha-device-id', 'connections': [['mac', 'aa:bb:cc:dd:ee:ff']], 'identifiers': []},
            ]}))
        async with serve(registry, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setattr(commands, 'connect', lambda url: real_connect(f'ws://127.0.0.1:{port}/api/websocket'))
            client = commands.HomeAssistantCommands('http://registry-test/api', 'test-token', language='en')
            await client.resolve_device('AA:BB:CC:DD:EE:FF')
            assert client.device_id == 'real-ha-device-id'
            await client.resolve_device('AA:BB:CC:DD:EE:FF')
            assert len(seen) == 1
    commands._DEVICE_CACHE.clear()
    asyncio.run(run())


@pytest.fixture
def processor(monkeypatch):
    # The command processor's boundary consists of context/speech frames. Use
    # small transport doubles; the matcher and HA HTTP client are real libraries.
    class Frame: pass
    class VADUserStartedSpeakingFrame(Frame): pass
    class LLMContextFrame(Frame):
        def __init__(self, messages):
            self.context = SimpleNamespace(get_messages=lambda: messages)
    class TTSSpeakFrame(Frame):
        def __init__(self, text): self.text = text
    class FrameProcessor:
        def __init__(self, **kwargs): self.sent = []
        async def process_frame(self, frame, direction): pass
        async def push_frame(self, frame, direction): self.sent.append((frame, direction))
    direction = SimpleNamespace(DOWNSTREAM='down', UPSTREAM='up')
    monkeypatch.setitem(sys.modules, 'pipecat.frames.frames', SimpleNamespace(
        Frame=Frame, LLMContextFrame=LLMContextFrame, TTSSpeakFrame=TTSSpeakFrame,
        VADUserStartedSpeakingFrame=VADUserStartedSpeakingFrame))
    monkeypatch.setitem(sys.modules, 'pipecat.processors.frame_processor', SimpleNamespace(
        FrameDirection=direction, FrameProcessor=FrameProcessor))
    module = load('fast_path.py', 'test_local_command_processor')
    return module, LLMContextFrame, direction


def test_complete_user_turn_is_absorbed_and_reply_goes_to_tts(processor):
    module, ContextFrame, direction = processor
    seen = []
    async def process(text):
        seen.append(text)
        return commands.LocalReply('Light off.')
    step = module.FastPathStep(state=None, language='en', bridge=None, tool_names={},
                               local_commands=SimpleNamespace(process=process))
    frame = ContextFrame([{'role': 'user', 'content': 'turn off the kitchen lights'}])
    asyncio.run(step.process_frame(frame, direction.DOWNSTREAM))
    assert seen == ['turn off the kitchen lights']
    assert len(step.sent) == 1 and step.sent[0][0].text == 'Light off.'


def test_no_match_and_tool_result_context_pass_to_llm_once(processor):
    module, ContextFrame, direction = processor
    seen = []
    async def process(text):
        seen.append(text)
        return None
    step = module.FastPathStep(state=None, language='en', bridge=None, tool_names={},
                               local_commands=SimpleNamespace(process=process))
    unmatched = ContextFrame([{'role': 'user', 'content': 'tell me a joke'}])
    tool_result = ContextFrame([{'role': 'user', 'content': 'turn off the light'},
                                {'role': 'tool', 'content': 'done'}])
    async def run():
        await step.process_frame(unmatched, direction.DOWNSTREAM)
        await step.process_frame(tool_result, direction.DOWNSTREAM)
    asyncio.run(run())
    assert seen == ['tell me a joke']
    assert step.sent == [(unmatched, direction.DOWNSTREAM), (tool_result, direction.DOWNSTREAM)]


def test_local_clarification_explicitly_reopens_a_follow_up(processor):
    module, ContextFrame, direction = processor
    async def process(text): return commands.LocalReply('Which room?', True)
    protocol = SimpleNamespace(continuation_requested=False)
    step = module.FastPathStep(state=None, language='en', bridge=None, tool_names={},
                               local_commands=SimpleNamespace(process=process), protocol=protocol)
    asyncio.run(step.process_frame(ContextFrame([{'role': 'user', 'content': 'turn it off'}]), direction.DOWNSTREAM))
    assert protocol.continuation_requested
    assert step.sent[0][0].text == 'Which room?'


def test_missing_second_transcript_cannot_replay_previous_request(processor):
    module, ContextFrame, direction = processor
    messages = [{'role': 'user', 'content': 'weather'}, {'role': 'assistant', 'content': 'Cloudy.'}]
    context = SimpleNamespace(get_messages=lambda: messages)
    step = module.FastPathStep(state=None, language='en', bridge=None, tool_names={}, context=context)
    async def run():
        await step.process_frame(module.VADUserStartedSpeakingFrame(), direction.DOWNSTREAM)
        step.sent.clear()
        await step.process_frame(ContextFrame(messages), direction.DOWNSTREAM)
    asyncio.run(run())
    assert step.sent == []


def test_same_context_cannot_execute_a_command_twice(processor):
    module, ContextFrame, direction = processor
    seen = []
    async def process(text):
        seen.append(text)
        return commands.LocalReply('Playing.')
    step = module.FastPathStep(state=None, language='en', bridge=None, tool_names={},
                               local_commands=SimpleNamespace(process=process))
    frame = ContextFrame([{'role': 'user', 'content': 'play music'}])
    async def run():
        await step.process_frame(frame, direction.DOWNSTREAM)
        await step.process_frame(frame, direction.DOWNSTREAM)
    asyncio.run(run())
    assert seen == ['play music']


def test_restricted_fallback_still_matches_real_ha_custom_sentence(processor, tmp_path):
    module, ContextFrame, direction = processor
    language = tmp_path/'en'
    language.mkdir()
    (language/'lights.yaml').write_text('''language: en
intents:
  HassTurnOff:
    data:
      - sentences: ["make the kitchen dark"]
        slots: {area: Kitchen, domain: light}
''', encoding='utf-8')
    intents, report = module.load_intents('en', [tmp_path])
    state = module.FastPathState(intents=intents, report=report)
    state.set_slot_lists(['Kitchen Light'], ['Kitchen'])
    seen = []
    async def call_tool(name, args):
        seen.append((name, args))
        return json.dumps({'response_type': 'action_done', 'data': {'success': [{'name': 'Kitchen Light'}]}})
    step = module.FastPathStep(state=state, language='en',
                               bridge=SimpleNamespace(call_tool=call_tool),
                               tool_names={'HassTurnOff': 'assist__intent__HassTurnOff'})
    asyncio.run(step.process_frame(ContextFrame([{'role': 'user', 'content': 'make the kitchen dark'}]), direction.DOWNSTREAM))
    assert seen == [('assist__intent__HassTurnOff', {'area': 'Kitchen', 'domain': ['light']})]
    assert step.sent[0][0].text == 'Turned off Kitchen Light.'


@pytest.mark.parametrize('restricted', [False, True])
def test_packaged_builder_preserves_mcp_allowlist(monkeypatch, restricted):
    tree = ast.parse((ROOT/'tools/pipecat_echomuse/package.py').read_text(encoding='utf-8'))
    source = next(node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                  and isinstance(node.value, str) and 'async def _build_fast_path_step(' in node.value)
    builder = next(node for node in ast.parse(source + '\n    pass\n').body
                   if isinstance(node, ast.AsyncFunctionDef) and node.name == '_build_fast_path_step')
    calls = []
    async def resolve(mac): calls.append(mac)
    def from_config(*args, **kwargs):
        calls.append('native')
        return SimpleNamespace(resolve_device=resolve)
    monkeypatch.setitem(sys.modules, 'app.local_commands', SimpleNamespace(from_config=from_config))
    namespace = {'fast_path': SimpleNamespace(FastPathStep=SimpleNamespace,
                                              tool_names_for_fast_path=lambda names: {})}
    code = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), builder], type_ignores=[])
    exec(compile(ast.fix_missing_locations(code), 'packaged local command builder', 'exec'), namespace)
    flow = SimpleNamespace(language='en', mcp_tool_allowlist=['HassTurnOn'] if restricted else [])
    result = asyncio.run(namespace['_build_fast_path_step'](
        SimpleNamespace(), flow, None, SimpleNamespace(standard_tools=[]), {'satellite_mac': 'aa:bb'}))
    assert calls == ([] if restricted else ['native', 'aa:bb'])
    assert (result is None) is restricted
