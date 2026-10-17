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
    assert {'EchoMuseStop','EchoMuseCreate'} <= set(allowlist)
    assert 'scheduler' not in second
    assert len(adapter.mcp_options(second,original,[])[0])==1
    controls=adapter.options({**headers,'x-echomuse-local-schedules':'false'},True)
    _,control_allowlist=adapter.mcp_options(controls,[],['HassTurnOn'])
    assert 'EchoMuseStop' in control_allowlist
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
