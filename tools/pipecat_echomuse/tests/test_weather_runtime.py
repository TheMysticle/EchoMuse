"""HA sentence grammar, forecast selection and actual Pipecat local routing."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import weather_commands as weather
import local_commands
from fast_path import FastPathStep
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import LLMContextFrame, TTSSpeakFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection


def data():
    return json.dumps({'result': {'daily_forecast': ' | '.join(
        f'{day.title()}: partlycloudy, High {10+i}.2° / Low -{i}.3° (Rain: {i*10}%)'
        for i, day in enumerate(weather.DAYS))}})


@pytest.mark.parametrize('text,day,rain', [
    ("what's the weather like tomorrow", 'tomorrow', False),
    ('What will the weather be like tomorrow?', 'tomorrow', False),
    ("what's the weather forecast tomorrow", 'tomorrow', False),
    ('what will the weather be like on Tuesday', 'tuesday', False),
    ('give me the forecast for the day after tomorrow', 'the day after tomorrow', False),
    ('will it rain tomorrow', 'tomorrow', True),
    ('is it going to rain on Friday', 'friday', True),
    ('forecast', 'today', False),
])
def test_custom_sentences(text, day, rain):
    assert weather.parse_request(text) == weather.WeatherRequest(day, rain)


def test_user_installed_ha_grammar_loads_without_bundled_copy(tmp_path, monkeypatch):
    import shutil
    grammar = Path(weather.__file__).parent/'custom_sentences'
    shutil.copytree(grammar, tmp_path/'ha/custom_sentences')
    (tmp_path/'app').mkdir()
    monkeypatch.setattr(weather, '__file__', str(tmp_path/'app/weather_commands.py'))
    monkeypatch.setattr(weather, 'Path', lambda value: (
        tmp_path/'ha/custom_sentences/en/weather.yaml'
        if str(value) == '/homeassistant/custom_sentences/en/weather.yaml' else Path(value)))
    weather._grammar.cache_clear()
    try:
        assert weather.parse_request('What will the weather be like tomorrow?') == weather.WeatherRequest('tomorrow')
    finally:
        weather._grammar.cache_clear()


def test_missing_bundled_grammar_reports_searched_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(weather, '__file__', str(tmp_path/'app/weather_commands.py'))
    weather._grammar.cache_clear()
    with pytest.raises(FileNotFoundError, match='Weather grammar missing; searched:'):
        weather.parse_request('What will the weather be like tomorrow?')


@pytest.mark.parametrize('text', [
    'turn off the bedroom light', "what's the weather like",
    'weather tomorrow and turn off the light', 'will it rain in London tomorrow',
    'forecast for next Tuesday', 'what about tomorrow',
])
def test_unmatched_phrases_are_not_forced_into_a_forecast(text):
    assert weather.parse_request(text) is None


@pytest.mark.parametrize('day,expected', [('today', 'Monday'), ('tomorrow', 'Tuesday'),
    ('the day after tomorrow', 'Wednesday'), ('friday', 'Friday')])
def test_requested_day_not_current_conditions(day, expected):
    speech = weather.forecast_reply(data(), weather.WeatherRequest(day), timezone='Europe/Warsaw',
                                    now=datetime(2026, 10, 19, 12, tzinfo=timezone.utc))
    assert speech.startswith(expected+': partly cloudy,')
    assert 'high of' in speech and 'low of' in speech


@pytest.mark.parametrize('date', ['2026-10-18T22:30:00+00:00', '2026-03-28T23:30:00+00:00'])
def test_relative_days_follow_ha_timezone_across_midnight_and_dst(date):
    from zoneinfo import ZoneInfo
    now = datetime.fromisoformat(date)
    speech = weather.forecast_reply(data(), weather.WeatherRequest('tomorrow'),
                                    timezone='Europe/Warsaw', now=now)
    expected = weather.DAYS[(now.astimezone(ZoneInfo('Europe/Warsaw')).weekday()+1) % 7].title()
    assert speech.startswith(expected+':')


def test_unknown_timezone_missing_day_and_duplicate_day_fall_back():
    assert weather.forecast_reply(data(), weather.WeatherRequest('tomorrow')) is None
    assert weather.forecast_reply(json.dumps({'result': {'daily_forecast': 'Tuesday: cloudy'}}),
                                  weather.WeatherRequest('tuesday')) is None
    entry = 'Tuesday: cloudy, High 10° / Low 2° (Rain: 5%)'
    assert weather.forecast_reply(json.dumps({'result': {'daily_forecast': entry+' | '+entry}}),
                                  weather.WeatherRequest('tuesday')) is None


def test_rain_answer_does_not_invent_certainty():
    speech = weather.forecast_reply(data(), weather.WeatherRequest('tuesday', True))
    assert speech == 'Tuesday: partly cloudy, with a reported 10 percent chance of rain.'


class Step(FastPathStep):
    async def push_frame(self, frame, direction):
        self.sent.append(frame)


@pytest.mark.parametrize('raw,expected_tts', [(data(), True), ('{"result": {}}', False), ('invalid JSON', False)])
def test_forecast_uses_one_script_call_and_never_current_weather_agent(raw, expected_tts):
    async def run():
        calls = []
        class Bridge:
            async def call_tool(self, name, args):
                calls.append((name, args))
                return raw
        class Local:
            async def weather_timezone(self):
                return 'Europe/Warsaw'
            async def process(self, text):
                raise AssertionError('A forecast must not be answered as current conditions')
        full = ToolsSchema(standard_tools=[FunctionSchema(name='assist__script__get_weather',
            description='Read current and future weather.', properties={}, required=[])])
        context = LLMContext([], tools=full)
        step = Step(state=None, language='en', bridge=Bridge(), tool_names={}, local_commands=Local(), context=context)
        step.sent = []
        context.add_message({'role': 'user', 'content': 'will it rain tomorrow'})
        frame = LLMContextFrame(context)
        await step.process_frame(frame, FrameDirection.DOWNSTREAM)
        assert calls == [('assist__script__get_weather', {})]
        assert isinstance(step.sent[-1], TTSSpeakFrame) == expected_tts
        if not expected_tts:
            assert step.sent == [frame]
    asyncio.run(run())


def test_current_weather_keeps_existing_native_path_without_extra_call():
    async def run():
        calls = []
        class Bridge:
            async def call_tool(self, *args):
                raise AssertionError('Current-weather route must not acquire another MCP hop')
        class Local:
            async def process(self, text):
                calls.append(text)
                return local_commands.LocalReply('8 degrees and cloudy.')
        context = LLMContext([], tools=ToolsSchema(standard_tools=[FunctionSchema(
            name='assist__script__get_weather', description='Weather', properties={}, required=[])]))
        step = Step(state=None, language='en', bridge=Bridge(), tool_names={}, local_commands=Local(), context=context)
        step.sent = []
        context.add_message({'role': 'user', 'content': "what's the weather like"})
        await step.process_frame(LLMContextFrame(context), FrameDirection.DOWNSTREAM)
        assert calls == ["what's the weather like"]
        assert step.sent[-1].text == '8 degrees and cloudy.'
    asyncio.run(run())


@pytest.mark.parametrize('text', ['Tell me a joke.', 'Explain photosynthesis.', 'Hello'])
def test_clear_chat_bypasses_ha_and_grammar_matching(text):
    async def run():
        class Local:
            async def process(self, text):
                raise AssertionError('Clear chat must not wait for HA')
        context = LLMContext([])
        step = Step(state=None, language='en', bridge=None, tool_names={},
                    local_commands=Local(), context=context)
        step.sent = []
        context.add_message({'role': 'user', 'content': text})
        frame = LLMContextFrame(context)
        await step.process_frame(frame, FrameDirection.DOWNSTREAM)
        assert step.sent == [frame]
    asyncio.run(run())


def test_timezone_lookup_is_cached_and_uses_same_authenticated_client(monkeypatch):
    calls = []
    real_client = httpx.AsyncClient
    def respond(request):
        calls.append(request)
        assert str(request.url) == 'http://ha/api/config'
        assert request.headers['Authorization'] == 'Bearer weather-test-token'
        return httpx.Response(200, json={'time_zone': 'Europe/Warsaw'})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    local_commands._TIMEZONE_CACHE.clear()
    async def run():
        client = local_commands.HomeAssistantCommands('http://ha/api', 'weather-test-token', language='en')
        try:
            assert await client.weather_timezone() == 'Europe/Warsaw'
            assert await client.weather_timezone() == 'Europe/Warsaw'
            assert len(calls) == 1
        finally:
            await client.aclose()
    asyncio.run(run())


def test_missing_weather_tool_keeps_forecast_on_llm_path():
    async def run():
        class Local:
            async def process(self, text):
                raise AssertionError('Do not substitute current weather for a future forecast')
        context = LLMContext([], tools=ToolsSchema(standard_tools=[]))
        step = Step(state=None, language='en', bridge=SimpleNamespace(), tool_names={},
                    local_commands=Local(), context=context)
        step.sent = []
        context.add_message({'role': 'user', 'content': "what's the weather forecast tomorrow"})
        frame = LLMContextFrame(context)
        await step.process_frame(frame, FrameDirection.DOWNSTREAM)
        assert step.sent == [frame]
    asyncio.run(run())


def test_unsupported_forecast_period_cannot_receive_current_conditions():
    async def run():
        class Local:
            async def process(self, text):
                raise AssertionError('A weekend forecast is not current weather')
        context = LLMContext([], tools=ToolsSchema(standard_tools=[]))
        step = Step(state=None, language='en', bridge=None, tool_names={}, local_commands=Local(), context=context)
        step.sent = []
        context.add_message({'role': 'user', 'content': "what's the weather like this weekend"})
        frame = LLMContextFrame(context)
        await step.process_frame(frame, FrameDirection.DOWNSTREAM)
        assert step.sent == [frame]
    asyncio.run(run())
