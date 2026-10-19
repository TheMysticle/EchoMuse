"""Real Pipecat contexts: turn isolation, expansion and provider request shape."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tool_scope import ToolScopeStep, EXPAND_TOOL, weather_request
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.adapters.services.open_ai_adapter import OpenAILLMAdapter
from pipecat.frames.frames import LLMContextFrame
from pipecat.processors.aggregators.llm_context import LLMContext, LLMSpecificMessage
from pipecat.processors.frame_processor import FrameDirection


def tools():
    return ToolsSchema(standard_tools=[FunctionSchema(name=name, description=name,
        properties={}, required=[]) for name in [
            'assist__script__get_weather', 'assist__intent__HassTurnOff',
            'assist__media_player__HassMediaUnpause', 'EchoMuseCreate', 'EchoMuseVolume',
            'assist__homeassistant__GetLiveContext', 'EchoMuseRequestContinuation']])


class Scope(ToolScopeStep):
    async def push_frame(self, frame, direction):
        pass


def names(context):
    return [tool.name for tool in context.tools.standard_tools]


async def user_turn(scope, context, text):
    context.add_message({'role': 'user', 'content': text})
    await scope.process_frame(LLMContextFrame(context), FrameDirection.DOWNSTREAM)


@pytest.mark.parametrize('text', ["what's the weather like", 'What is the weather tomorrow?',
    'Will it rain tonight?', 'weather', 'Please give me the forecast this weekend.'])
def test_standalone_weather_is_recognized(text):
    assert weather_request(text, 'en-US')


@pytest.mark.parametrize('text', ["what's the weather and turn off the bedroom lights",
    'weather, then play music', 'what about tomorrow', 'is it warmer there',
    'remind me to check the weather', 'weather in London', 'tell me a joke'])
def test_ambiguous_followup_and_multiple_requests_keep_full_tools(text):
    async def run():
        full = tools()
        context = LLMContext([], tools=full)
        scope = Scope(context=context)
        await user_turn(scope, context, text)
        assert context.tools is full
    asyncio.run(run())


def test_scoping_is_per_user_turn_and_tool_continuations_do_not_reset_it():
    async def run():
        full = tools()
        context = LLMContext([], tools=full)
        scope = Scope(context=context)
        await user_turn(scope, context, "what's the weather like")
        assert names(context) == ['assist__script__get_weather', 'EchoMuseRequestContinuation', EXPAND_TOOL]
        assert context.tools.standard_tools[0] is full.standard_tools[0]
        selected = context.tools
        context.add_message({'role': 'tool', 'content': 'Cloudy', 'tool_call_id': 'weather-1'})
        await scope.process_frame(LLMContextFrame(context), FrameDirection.UPSTREAM)
        await scope.process_frame(LLMContextFrame(context), FrameDirection.DOWNSTREAM)
        assert context.tools is selected
        context.add_message(LLMSpecificMessage(llm='google', message={'role': 'model'}))
        await user_turn(scope, context, 'turn off the bedroom light')
        assert context.tools is full
        await user_turn(scope, context, 'will it rain tomorrow')
        assert context.tools is not full
    asyncio.run(run())


def test_expansion_restores_only_configured_tools_and_stays_expanded_until_next_turn():
    async def run():
        full = tools()
        context = LLMContext([], tools=full)
        scope = Scope(context=context)
        await user_turn(scope, context, 'weather tomorrow')
        expand = context.tools.standard_tools[-1].handler
        results = []
        async def result(value):
            results.append(value)
        await expand(SimpleNamespace(result_callback=result))
        assert context.tools is full and results[0]['expanded'] is True
        await scope.process_frame(LLMContextFrame(context), FrameDirection.DOWNSTREAM)
        assert context.tools is full
        await user_turn(scope, context, 'weather tomorrow')
        selected = context.tools
        await expand(SimpleNamespace(result_callback=result))
        assert context.tools is selected and results[-1]['expanded'] is False
    asyncio.run(run())


@pytest.mark.parametrize('language', ['pl', 'de'])
def test_other_languages_keep_full_tools(language):
    async def run():
        full = tools()
        context = LLMContext([], tools=full)
        scope = Scope(context=context, language=language)
        await user_turn(scope, context, 'weather tomorrow')
        assert context.tools is full
    asyncio.run(run())


def test_no_weather_tool_and_custom_adapter_tools_are_preserved():
    async def run():
        for full in [ToolsSchema(standard_tools=tools().standard_tools[1:]),
                     ToolsSchema(standard_tools=tools().standard_tools, custom_tools={'provider': [{'search': {}}]})]:
            context = LLMContext([], tools=full)
            scope = Scope(context=context)
            await user_turn(scope, context, 'weather tomorrow')
            assert context.tools is full
    asyncio.run(run())


def test_explicit_tool_choice_is_preserved():
    async def run():
        full = tools()
        choice = {'type': 'function', 'function': {'name': 'assist__intent__HassTurnOff'}}
        context = LLMContext([], tools=full, tool_choice=choice)
        scope = Scope(context=context)
        await user_turn(scope, context, 'weather tomorrow')
        assert context.tools is full and context.tool_choice == choice
    asyncio.run(run())


def test_openai_compatible_provider_payload_is_smaller_without_removing_history():
    async def run():
        full = tools()
        context = LLMContext([{'role': 'assistant', 'content': 'Previous answer.'}], tools=full)
        scope = Scope(context=context)
        context.add_message({'role': 'user', 'content': "what's the weather like"})
        adapter = OpenAILLMAdapter()
        before = adapter.get_llm_invocation_params(context, system_instruction='Keep replies brief.', convert_developer_to_user=True)
        await scope.process_frame(LLMContextFrame(context), FrameDirection.DOWNSTREAM)
        after = adapter.get_llm_invocation_params(context, system_instruction='Keep replies brief.', convert_developer_to_user=True)
        assert len(after['tools']) == 3
        assert len(json.dumps(after['tools'])) < len(json.dumps(before['tools']))
        assert before['messages'] == after['messages']
    asyncio.run(run())


@pytest.mark.parametrize('base_url,model', [
    ('https://api.cerebras.ai/v1', 'gpt-oss-120b'),
    ('https://api.groq.com/openai/v1', 'llama-3.3-70b-versatile'),
])
def test_real_model_tool_loop_can_expand_and_resume(base_url, model):
    import httpx
    from openai import AsyncOpenAI
    from pipecat.frames.frames import EndFrame, LLMTextFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineWorker
    from pipecat.workers.runner import WorkerRunner
    from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
    from pipecat.processors.frame_processor import FrameProcessor
    from pipecat.services.openai.llm import OpenAILLMService

    async def run():
        full = tools()
        context = LLMContext([], tools=full)
        ready, finished = asyncio.Event(), asyncio.Event()
        seen = []
        def respond(request):
            body = json.loads(request.content)
            seen.append(body)
            assert str(request.url) == base_url+'/chat/completions'
            if len(seen) == 1:
                assert [t['function']['name'] for t in body['tools']] == [
                    'assist__script__get_weather', 'EchoMuseRequestContinuation', EXPAND_TOOL]
                delta = {'tool_calls': [{'index': 0, 'id': 'expand-1', 'type': 'function',
                    'function': {'name': EXPAND_TOOL, 'arguments': '{}'}}]}
                reason = 'tool_calls'
            else:
                assert len(seen) == 2
                assert {t['function']['name'] for t in body['tools']} == {t.name for t in full.standard_tools}
                assert any(m.get('role') == 'tool' and 'true' in str(m.get('content')) for m in body['messages'])
                delta, reason = {'content': 'All configured tools are available.'}, 'stop'
            chunk = {'id': 'chat-test', 'object': 'chat.completion.chunk', 'created': 0,
                     'model': model, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': reason}]}
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                text='data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n')
        llm = OpenAILLMService(api_key='test-key', base_url=base_url,
            settings=OpenAILLMService.Settings(model=model, system_instruction='Be brief.'))
        original = llm._client
        llm._client = AsyncOpenAI(api_key='test-key', base_url=base_url,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        await original.close()
        pair = LLMContextAggregatorPair(context)
        class Sink(FrameProcessor):
            async def process_frame(self, frame, direction):
                await super().process_frame(frame, direction)
                from pipecat.frames.frames import StartFrame
                if isinstance(frame, StartFrame):
                    ready.set()
                if isinstance(frame, LLMTextFrame) and frame.text == 'All configured tools are available.':
                    finished.set()
                await self.push_frame(frame, direction)
        worker = PipelineWorker(Pipeline([pair.user(), ToolScopeStep(context=context), llm, Sink(), pair.assistant()]),
                                idle_timeout_secs=None)
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        running = asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(ready.wait(), 5)
            context.add_message({'role': 'user', 'content': 'weather tomorrow'})
            await worker.queue_frame(LLMContextFrame(context))
            await asyncio.wait_for(finished.wait(), 5)
            assert len(seen) == 2
        finally:
            await worker.queue_frame(EndFrame())
            await asyncio.wait_for(running, 5)
    asyncio.run(run())
