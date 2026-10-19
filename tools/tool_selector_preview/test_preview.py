"""Routing and HTTP checks use the exact module served by the preview."""
import importlib.util
import json
from pathlib import Path
import sys
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent/'pipecat_echomuse'))
from tool_selector import select_tools, catalogue_names

TOOLS = ['ha__HassTurnOn', 'ha__HassTurnOff', 'ha__HassLightSet',
         'ha__script__get_weather', 'ha__HassMediaPause', 'EchoMuseSpotifySearchAndPlay',
         'EchoMuseCreate', 'EchoMuseList', 'EchoMuseAction', 'EchoMuseRequestContinuation',
         'ha__GetLiveContext', 'ha__HassClimateSetTemperature', 'ha__HassCoverOpen']


@pytest.mark.parametrize('sentence,categories', [
    ('Dim the lights in the living room to 50 percent and turn off the lights in the bedroom', ['lighting']),
    ('Dim the living room lights to 50% and play music in the bedroom', ['lighting', 'music']),
    ('What will the weather be like tomorrow?', ['weather']),
    ('Set the bedroom thermostat to 21 degrees', ['climate']),
    ('Close the bedroom blinds', ['covers']),
    ('Set a timer for five minutes', ['schedules']),
    ('Please turn off the bedroom lights please', ['lighting']),
])
def test_multi_category_selection(sentence, categories):
    result = select_tools(sentence, TOOLS)
    assert result['mode'] == 'scoped'
    assert result['categories'] == categories
    assert 'EchoMuseRequestContinuation' in result['selected_tools']
    assert 'ha__GetLiveContext' in result['selected_tools']
    assert result['excluded_tools']
    assert result['expansion_required']


@pytest.mark.parametrize('sentence', [
    'Turn off the bedroom lights and lock the front door',
    'And the bedroom?', 'If it rains tomorrow, turn off the bedroom lights',
    'Play music then open the garage door', '',
    'Tell me a joke', 'turn off the bedroom lights and',
])
def test_incomplete_or_unknown_clauses_preserve_all_tools(sentence):
    result = select_tools(sentence, TOOLS)
    assert result['mode'] == 'full'
    assert result['selected_tools'] == TOOLS
    assert result['excluded_tools'] == []


def test_missing_capability_and_unsupported_language_keep_full_tools():
    assert select_tools('play music', ['ha__HassLightSet'])['mode'] == 'full'
    assert select_tools('play music', TOOLS, 'pl')['mode'] == 'full'


def test_unknown_custom_tools_preserved_and_allowlist_never_expanded():
    allowed = ['ha__script__get_weather', 'my_custom_tool']
    result = select_tools('weather tomorrow', allowed)
    assert result['selected_tools'] == allowed
    assert result['unmapped_tools'] == ['my_custom_tool']
    assert set(result['selected_tools']) <= set(allowed)


def test_tool_definitions_and_namespaces():
    assert catalogue_names({'result': {'tools': [{'name': 'ha__HassLightSet'}]}}) == ['ha__HassLightSet']
    assert catalogue_names([{'type': 'function', 'function': {'name': 'HassLightSet'}}]) == ['HassLightSet']
    assert catalogue_names(['HassLightSet', 'HassLightSet']) == ['HassLightSet']
    with pytest.raises(ValueError):
        catalogue_names([{'description': 'no name'}])


def test_preview_http_runs_shared_selector_and_rejects_bad_json():
    spec = importlib.util.spec_from_file_location('selector_preview_server', ROOT/'serve.py')
    server_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server_module)
    server = server_module.ThreadingHTTPServer(('127.0.0.1', 0), server_module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_address[1]}'
    try:
        with urlopen(base) as response:
            assert b'Tool selector lab' in response.read()
        payload = {'text': 'turn off the bedroom lights and play music', 'catalogue': TOOLS}
        with urlopen(Request(base+'/api/select', data=json.dumps(payload).encode())) as response:
            result = json.load(response)
        expected = select_tools(payload['text'], TOOLS)
        assert {k: v for k, v in result.items() if k != 'elapsed_ms'} == {
            k: v for k, v in expected.items() if k != 'elapsed_ms'}
        with pytest.raises(HTTPError) as error:
            urlopen(Request(base+'/api/select', data=b'{bad'))
        assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
