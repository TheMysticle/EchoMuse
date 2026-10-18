"""Pinned upstream adapter, authentication, isolation and continuation contract."""
import ast
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT=Path(__file__).resolve().parents[2]


def load(path, name):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module


adapter=load(ROOT/'tools/pipecat_echomuse/echomuse.py','echo_pipecat_adapter_test')


def test_profiles_require_sustained_speech_and_do_not_edit_fallback():
    assert adapter.PROFILES['balanced']=={'confidence':.8,'start_secs':.3,'min_volume':.04}
    assert adapter.PROFILES['low']['start_secs']==.45
    assert adapter.PROFILES['responsive']['start_secs']==.12
    marker=object()
    assert adapter.vad_analyzer({},lambda:marker) is marker


def test_credentials_are_session_scoped_and_require_satellite_authentication():
    headers={'x-echomuse-barge-in':'low','x-echomuse-scheduler-url':'http://controller:8768/api/devices/A/voice-workflow/mcp','x-echomuse-scheduler-token':'emvoice.A.test','x-echomuse-local-schedules':'true'}
    assert adapter.options(headers,False)=={}
    assert adapter.options({},True)=={}
    first=adapter.options(headers,True)
    second=adapter.options({'x-echomuse-barge-in':'balanced'},True)
    original=[{'id':'homeassistant','url':'http://ha/api/mcp'}]
    servers,allowlist=adapter.mcp_options(first,original,['HassTurnOn'])
    assert len(servers)==2 and len(original)==1
    assert {'EchoMuseStop','EchoMuseVolume','EchoMuseCreate'} <= set(allowlist)
    assert 'scheduler' not in second
    assert len(adapter.mcp_options(second,original,[])[0])==1
    controls=adapter.options({**headers,'x-echomuse-local-schedules':'false'},True)
    _,control_allowlist=adapter.mcp_options(controls,[],['HassTurnOn'])
    assert 'EchoMuseStop' in control_allowlist and 'EchoMuseVolume' in control_allowlist
    assert 'EchoMuseCreate' not in control_allowlist
    with pytest.raises(ValueError):
        adapter.options({**headers,'x-echomuse-scheduler-url':'http://user:secret@controller/api/devices/B/voice-workflow/mcp'},True)


@pytest.mark.parametrize('answer', ['Done?', 'Which room?', 'Your reminder is saved.'])
def test_continuation_requires_explicit_request_not_reply_punctuation(answer):
    path=ROOT/'dist/pipecat-assist-echomuse-dev/app/va_pipecat_protocol.py'
    if not path.exists():
        pytest.skip('Package pinned Pipecat source for protocol integration tests')
    protocol=load(path,'echo_pipecat_protocol_test')
    obj=protocol.VaPipecatProtocol(lambda *args:False)
    obj.on_rtvi_message({'label':'rtvi-ai','type':'bot-output','data':{'text':answer}})
    event=json.loads(obj.on_rtvi_message({'label':'rtvi-ai','type':'bot-stopped-speaking'}))
    assert event['follow_up'] and event['continuation_question'] is False


def test_explicit_continuation_is_emitted_once_and_reset_for_next_turn():
    path=ROOT/'dist/pipecat-assist-echomuse-dev/app/va_pipecat_protocol.py'
    if not path.exists():
        pytest.skip('Package pinned Pipecat source for protocol integration tests')
    protocol=load(path,'echo_pipecat_protocol_test')
    obj=protocol.VaPipecatProtocol(lambda *args:False)
    obj.continuation_requested=True
    event=json.loads(obj.on_rtvi_message({'label':'rtvi-ai','type':'bot-stopped-speaking'}))
    assert event['continuation_question'] is True
    obj.client_action('{"type":"wake"}')
    obj.on_rtvi_message({'label':'rtvi-ai','type':'bot-output','data':{'text':'All done?'}})
    event=json.loads(obj.on_rtvi_message({'label':'rtvi-ai','type':'bot-stopped-speaking'}))
    assert event['continuation_question'] is False


def test_generated_adapter_registers_per_connection_continuation_tool():
    path=ROOT/'dist/pipecat-assist-echomuse-dev/app/main.py'
    if not path.exists():
        pytest.skip('Package pinned Pipecat source for adapter integration tests')
    source=path.read_text()
    ast.parse(source)
    assert 'continuation_schema(protocol)' in source
    assert 'EchoMuseRequestContinuation' in (ROOT/'tools/pipecat_echomuse/echomuse.py').read_text()
    assert 'assistant_text.rstrip().endswith' not in (path.parent/'va_pipecat_protocol.py').read_text()


def test_generated_adapter_tells_the_model_which_language_to_reply_in():
    # Gemini Live treats the configured language as a hint and follows the
    # speaker, so a household with other-language names got replies in them.
    path=ROOT/'dist/pipecat-assist-echomuse-dev/app/main.py'
    if not path.exists():
        pytest.skip('Package pinned Pipecat source for adapter integration tests')
    source=path.read_text()
    assert 'lang_hint' in source and 'flow.language' in source
    assert source.count('+ area_hint + lang_hint')==1


def test_generated_adapter_forbids_closing_offers_and_names_them():
    # The model ended "Light off." with "Can I help with anything else?" on every
    # command. The old rule was an abstract clause at the start of a long
    # paragraph; the habit has to be named, and the clarification path kept.
    path=ROOT/'dist/pipecat-assist-echomuse-dev/app/main.py'
    if not path.exists():
        pytest.skip('Package pinned Pipecat source for adapter integration tests')
    source=path.read_text()
    ast.parse(source)
    assert 'Never end a reply with an offer or a question' in source
    assert 'Can I help with anything else?' in source
    assert 'Do not close any reply by offering more help' in source
    # Asking is still allowed when something required is missing, via the tool.
    assert 'call EchoMuseRequestContinuation before asking' in source


@pytest.mark.parametrize('given,expected_first',[
    ('Living_Room','Living Room'),         # the blend seen in the field
    ('living-room','Living Room'),
    ('LIVING ROOM','Living Room'),
    ('Living  Room ','Living Room'),
])
def test_area_spellings_start_with_the_name_home_assistant_matches(given,expected_first):
    spellings=adapter.area_spellings(given)
    assert spellings[0].casefold()==expected_first.casefold() and ' ' in spellings[0]
    assert 'living_room' in spellings and given not in spellings


def test_area_spellings_of_nothing_are_nothing():
    assert adapter.area_spellings('')==[] and adapter.area_spellings(' _ ')==[]


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_an_unknown_area_spelling_is_retried_and_the_model_sees_only_the_success():
    calls=[]
    async def call(arguments):
        calls.append(arguments.get('area'))
        # Home Assistant: the name ignoring case, or the exact id.
        if arguments.get('area') in ('Living Room','living room','Living room','living_room'):
            return '{"response_type": "action_done"}'
        return f"Error calling tool: The area does not exist (given area '{arguments['area']}', domains light)"
    result=_run(adapter.call_with_area_fallback(call,{'area':'Living_Room','domain':['light']}))
    assert 'action_done' in result
    assert calls==['Living_Room','Living Room']


def test_a_genuinely_unknown_area_is_still_reported():
    async def call(arguments):
        return f"Error calling tool: The area does not exist (given area '{arguments['area']}')"
    result=_run(adapter.call_with_area_fallback(call,{'area':'Garage'}))
    assert result.endswith("(given area 'Garage')")


def test_calls_without_an_area_or_with_other_errors_are_not_retried():
    calls=[]
    async def call(arguments):
        calls.append(arguments)
        return 'Error calling tool: No device named lamp'
    assert _run(adapter.call_with_area_fallback(call,{'name':'lamp'}))
    assert _run(adapter.call_with_area_fallback(call,{'area':'Kitchen','name':'lamp'}))
    assert len(calls)==2


def test_generated_adapter_retries_area_spellings_and_tells_the_model_not_to_ask():
    root=ROOT/'dist/pipecat-assist-echomuse-dev/app'
    if not (root/'mcp_bridge.py').exists():
        pytest.skip('Package pinned Pipecat source for adapter integration tests')
    bridge=(root/'mcp_bridge.py').read_text()
    ast.parse(bridge)
    assert 'echomuse.call_with_area_fallback(_call, arguments)' in bridge
    main=(root/'main.py').read_text()
    assert 'do not ask the user which area it is in' in main
    assert "never join its words with underscores" in main
    assert "A spoken volume number is a percentage" in main
    assert "call EchoMuseVolume with that percentage; it changes only this speaker" in main
    assert "stop or pause said of the music with no device or room named" in main


class _Params:
    def __init__(self, llm):
        self.llm=llm
        self.tool_call_id='fc_1'
        self.function_name='EchoMuseRequestContinuation'
        self.calls=[]

    async def result_callback(self, result, properties=None):
        self.calls.append((result, properties))


class _GeminiLive:
    """The two private members of Pipecat's Gemini Live service the handler uses."""
    def __init__(self):
        self.sent=[]
        self._completed_tool_calls=set()

    async def _tool_result(self, tool_call_id, name, message):
        self.sent.append((tool_call_id, name, message))


class _Protocol:
    continuation_requested=False


def test_continuation_result_reaches_a_blocking_model_before_the_bot_stops_speaking():
    # Gemini 3 Live calls the tool in the middle of its reply and blocks on the
    # result. Pipecat's aggregator holds a result until the bot stops speaking,
    # which stalled the reply's audio for 3s and left the turn open (2026-10-18).
    import asyncio
    llm=_GeminiLive()
    params=_Params(llm)
    protocol=_Protocol()
    marker=object()
    asyncio.run(adapter.answer_continuation(protocol, params, lambda: marker))
    assert protocol.continuation_requested is True
    assert llm.sent==[('fc_1','EchoMuseRequestContinuation',{'value':adapter.CONTINUATION_RESULT})]
    # Marked done so the aggregator's later context push does not send it twice,
    # and told not to run the model again.
    assert llm._completed_tool_calls=={'fc_1'}
    assert params.calls==[(adapter.CONTINUATION_RESULT, marker)]


def test_continuation_keeps_the_normal_path_on_a_service_without_the_private_hooks():
    import asyncio
    params=_Params(object())
    protocol=_Protocol()
    asyncio.run(adapter.answer_continuation(protocol, params, lambda: pytest.fail('no override expected')))
    assert protocol.continuation_requested is True
    assert params.calls==[(adapter.CONTINUATION_RESULT, None)]
