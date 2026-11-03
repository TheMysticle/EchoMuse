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


# --- the household's time from Gemini's own UTC clock ----------------------
# 2026-11-03: asked the time in Warsaw at 21:47 CET, Gemini Live answered
# exactly an hour behind with no time in its context: its clock is UTC.

def test_the_clock_rule_gives_the_offset_and_the_next_change():
    import echomuse
    from datetime import datetime, timezone
    winter = echomuse.clock_rule("Europe/Warsaw", datetime(2026, 11, 3, 20, 47, tzinfo=timezone.utc))
    assert "Your own clock runs on UTC; household time is UTC+1 until 28 March 2027 01:00 UTC, then UTC+2" in winter
    assert "never say a UTC time" in winter
    summer = echomuse.clock_rule("Europe/Warsaw", datetime(2026, 7, 1, tzinfo=timezone.utc))
    assert "UTC+2 until 25 October 2026 01:00 UTC, then UTC+1" in summer


def test_a_zone_without_clock_changes_and_a_half_hour_offset():
    import echomuse
    rule = echomuse.clock_rule("Asia/Kolkata")
    assert "household time is UTC+5:30." in rule and "until" not in rule
    assert "UTC-3" in echomuse.clock_rule("America/Sao_Paulo")


def test_no_time_is_ever_written_into_the_prompt():
    # A Live session resumes for hours: a written time would go stale.
    import echomuse, re
    rule = echomuse.timezone_rule({"timezone": "Europe/Warsaw"})
    assert not re.search(r"\b\d{1,2}:\d\d\b(?! UTC)", rule)


def test_an_unknown_zone_falls_back_to_getdatetime():
    import echomuse
    assert "call GetDateTime" in echomuse.timezone_rule({})
    assert echomuse.clock_rule("Not/AZone").startswith("For any question about the current time")
