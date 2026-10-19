"""Cerebras packaging and real OpenAI SDK streaming/tool-call contract."""
import ast
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.processors.aggregators.llm_context import LLMContext

PACKAGE = Path(__file__).resolve().parents[1]/'package.py'
spec = importlib.util.spec_from_file_location('cerebras_packager', PACKAGE)
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)


def replacement(prefix):
    return next(node.args[1].value for node in ast.walk(ast.parse(PACKAGE.read_text(encoding='utf-8')))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'replace'
                and len(node.args) == 2 and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str) and node.args[1].value.startswith(prefix))


def service(base_url='', model='', default_model='gpt-oss-120b', key='cerebras-test-key'):
    source = replacement('    if integration.kind == "cerebras":')
    namespace = {'_effective_instructions': lambda flow: 'Keep replies brief.'}
    exec('def build(step, integration, flow):\n'+source, namespace)
    return namespace['build'](SimpleNamespace(model=model),
        SimpleNamespace(kind='cerebras', api_key=key, base_url=base_url, default_model=default_model),
        SimpleNamespace(max_output_tokens=128))


@pytest.mark.parametrize('base_url,model,default_model,expected_url,expected_model', [
    ('', '', '', 'https://api.cerebras.ai/v1/chat/completions', 'gpt-oss-120b'),
    ('', 'qwen-3.8-27b', 'gpt-oss-120b', 'https://api.cerebras.ai/v1/chat/completions', 'qwen-3.8-27b'),
    ('https://cerebras-proxy.test/inference/v1', '', 'custom-model',
     'https://cerebras-proxy.test/inference/v1/chat/completions', 'custom-model'),
])
def test_streaming_and_tools_use_cerebras_endpoint(base_url, model, default_model, expected_url, expected_model):
    async def run():
        llm = service(base_url, model, default_model)
        seen = []
        def respond(request):
            seen.append(json.loads(request.content))
            assert str(request.url) == expected_url
            assert request.headers['Authorization'] == 'Bearer cerebras-test-key'
            chunk = {'id': 'chat-test', 'object': 'chat.completion.chunk', 'created': 0,
                     'model': expected_model, 'choices': [{'index': 0, 'delta': {'content': 'Done.'}, 'finish_reason': None}]}
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                                  text='data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n')
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        original = llm._client
        llm._client = AsyncOpenAI(api_key='cerebras-test-key', base_url=original.base_url, http_client=client)
        await original.close()
        try:
            context = LLMContext([{'role': 'user', 'content': 'Turn off the bedroom light.'}],
                tools=ToolsSchema(standard_tools=[FunctionSchema(name='HassTurnOff', description='Turn a light off.',
                    properties={'name': {'type': 'string'}}, required=['name'])]))
            stream = await llm.get_chat_completions(context)
            chunks = [chunk async for chunk in stream]
            assert chunks[0].choices[0].delta.content == 'Done.'
            assert seen[0]['model'] == expected_model
            assert seen[0]['stream'] is True
            assert seen[0]['tools'][0]['function']['name'] == 'HassTurnOff'
            assert seen[0]['max_tokens'] == 128
            assert sum(m.get('content') == 'Keep replies brief.' for m in seen[0]['messages']) == 1
        finally:
            await llm._client.close()
    asyncio.run(run())


def test_missing_cerebras_key_does_not_fall_back_to_openai():
    with pytest.raises(RuntimeError, match='Cerebras is missing api_key'):
        service(key='')


@pytest.mark.parametrize('base_url', ['', 'https://cerebras-proxy.test/v1/'])
def test_model_discovery_preserves_all_cerebras_ids(monkeypatch, base_url):
    block = replacement('        if integration.kind == "cerebras" and capability')
    block = block[:block.rfind('        if integration.kind in {"openai", "openai_cloud"}')]
    source = 'async def models(integration, capability):\n'+block+'        return None\n'
    namespace = {'httpx': httpx}
    exec(source, namespace)
    real_client = httpx.AsyncClient
    def respond(request):
        assert str(request.url) == (base_url or 'https://api.cerebras.ai/v1').rstrip('/')+'/models'
        assert request.headers['Authorization'] == 'Bearer test-key'
        return httpx.Response(200, json={'data': [{'id': 'gpt-oss-120b'}, {'id': 'qwen-3.8-27b'}, {'id': 'future-model'}]})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    result = asyncio.run(namespace['models'](SimpleNamespace(kind='cerebras', api_key='test-key', base_url=base_url), 'llm'))
    assert {m['id'] for m in result['models']} == {'gpt-oss-120b', 'qwen-3.8-27b', 'future-model'}


def test_text_route_uses_own_key_and_endpoint():
    tree = ast.parse(PACKAGE.read_text(encoding='utf-8'))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'adapt_text_agent')
    changes = ast.literal_eval(next(n.value for n in function.body if isinstance(n, ast.Assign)))
    key_branch = next(new for old, new in changes if 'api_key =' in old)
    namespace = {}
    exec('def key(provider_kind, integration):\n'+key_branch+' "unused"\n    return api_key\n', namespace)
    assert namespace['key']('cerebras', SimpleNamespace(api_key='')) == ''
    assert namespace['key']('cerebras', SimpleNamespace(api_key='cb-key')) == 'cb-key'
    endpoint = next(new for old, new in changes if old.strip() == 'client = AsyncOpenAI(**client_kwargs)')
    # Compile the exact injected text-route block, without making a model call.
    namespace = {'AsyncOpenAI': lambda **kwargs: kwargs}
    exec('def client(provider_kind, integration, client_kwargs):\n'+endpoint+'\n            return client\n', namespace)
    assert namespace['client']('cerebras', SimpleNamespace(base_url=''), {'api_key': 'cb-key'}) == {
        'api_key': 'cb-key', 'base_url': 'https://api.cerebras.ai/v1'}
