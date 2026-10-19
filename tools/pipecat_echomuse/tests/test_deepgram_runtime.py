"""Reproduce Pipecat's filtering default and verify the actual SDK options."""
import ast
from pathlib import Path

from pipecat.services.deepgram.stt import DeepgramSTTService


def test_composed_deepgram_preserves_literal_titles_instead_of_censoring_them():
    package = Path(__file__).resolve().parents[1]/'package.py'
    replacement = next(node.args[1].value for node in ast.walk(ast.parse(package.read_text(encoding='utf-8')))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'replace'
        and len(node.args) == 2 and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
        and node.args[1].value.startswith('settings=DeepgramSTTService.Settings('))
    original = DeepgramSTTService(api_key='test-key',
        settings=DeepgramSTTService.Settings(model='nova-3'))
    namespace = {'DeepgramSTTService': DeepgramSTTService, 'model': 'nova-3'}
    exec("service = DeepgramSTTService(api_key='test-key', " + replacement + ')', namespace)
    before = original._build_connect_kwargs()
    after = namespace['service']._build_connect_kwargs()
    # Verify this in the real service, not just its Settings dataclass: the
    # constructor merges defaults, and the SDK expects a string query value.
    assert before['profanity_filter'] == 'true'
    assert after['profanity_filter'] == 'false'
    assert {k: v for k, v in before.items() if k != 'profanity_filter'} == {
        k: v for k, v in after.items() if k != 'profanity_filter'}
