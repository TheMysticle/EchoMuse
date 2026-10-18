"""Prompt wiring through Pipecat's real OpenAI-compatible adapter (Groq)."""
import ast
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from echomuse import composed_instructions
from pipecat.adapters.services.open_ai_adapter import OpenAILLMAdapter
from pipecat.processors.aggregators.llm_context import LLMContext


def context_seed(graph, instructions):
    tree = ast.parse((Path(__file__).resolve().parents[1]/'package.py').read_text(encoding='utf-8'))
    replacement = next(n.args[1].value for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'replace'
        and len(n.args) == 2 and isinstance(n.args[1], ast.Constant)
        and isinstance(n.args[1].value, str)
        and n.args[1].value.startswith('        context_messages = '))
    value = ast.parse(replacement.strip()).body[0].value
    return eval(compile(ast.Expression(value), '<context seed>', 'eval'), {
        '_flow_enabled': lambda flow: graph, '_effective_instructions': lambda flow: instructions,
        'flow': SimpleNamespace(),
    })


def test_composed_groq_request_contains_one_policy_and_preserves_greeting_and_history():
    policy = 'User configured instructions.' + composed_instructions(
        {'area': 'Bedroom', 'scheduler': {'schedules': True}}, 'en')
    messages = context_seed(False, policy)
    messages.extend([{'role': 'developer', 'content': 'User configured greeting.'},
                     {'role': 'user', 'content': 'Turn off the bedroom light.'}])
    params = OpenAILLMAdapter().get_llm_invocation_params(
        LLMContext(messages), system_instruction=policy, convert_developer_to_user=True)
    sent = params['messages']
    assert sum(message.get('content') == policy for message in sent) == 1
    assert sent[0] == {'role': 'system', 'content': policy}
    assert any(message.get('content') == 'User configured greeting.' for message in sent)
    assert sent[-1]['content'] == 'Turn off the bedroom light.'


def test_flow_graph_context_role_lifecycle_is_retained():
    assert context_seed(True, 'Graph instructions') == [
        {'role': 'developer', 'content': 'Graph instructions'}]


def test_prompt_only_refers_to_enabled_scheduler_tools():
    plain = composed_instructions({'area': 'Bedroom'}, 'pl')
    controls = composed_instructions({'scheduler': {'schedules': False}}, 'pl')
    schedules = composed_instructions({'scheduler': {'schedules': True}}, 'pl')
    assert 'EchoMuseStop' not in plain and 'EchoMuseVolume' not in plain
    assert 'EchoMuseList' not in controls and 'scheduler tools' not in controls
    assert 'EchoMuseStop' in controls and 'EchoMuseVolume' in controls
    assert 'EchoMuseList' in schedules and 'device acknowledgment' in schedules
    assert 'Reply in pl' in plain and 'Speaker area: Bedroom' in plain
