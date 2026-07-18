"""Regression (P1 finalization): AnthropicClient._generate_with_tools' error-fallback
path (which retries via non-tool _generate) fabricated an all-None usage dict instead
of re-extracting the real usage from the successful fallback call — dropping that
turn's tokens from billing/compaction. It must return self._extract_usage_data() on
BOTH the success and the fallback return paths.
"""
import ast
import inspect

from modules.llm.anthropic_client import AnthropicClient


def _method(cls, name):
    tree = ast.parse(inspect.getsource(cls))
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(name)


def test_fallback_uses_real_usage_extraction():
    m = _method(AnthropicClient, "_generate_with_tools")
    # Both return paths (success + error-fallback) must call _extract_usage_data.
    extract_calls = [
        n for n in ast.walk(m)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_extract_usage_data"
    ]
    assert len(extract_calls) >= 2, (
        "the error-fallback return must re-extract real usage, not fabricate all-None"
    )
    # And no all-None fabricated usage dict remains.
    for node in ast.walk(m):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            if set(keys) >= {"prompt_tokens", "completion_tokens", "total_tokens"}:
                vals_all_none = all(
                    isinstance(v, ast.Constant) and v.value is None for v in node.values
                )
                assert not vals_all_none, "all-None fabricated usage dict must be gone"
