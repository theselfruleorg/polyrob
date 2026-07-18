"""Regression (P1 finalization): GeminiClient._generate_with_tools must return the
3-tuple (text, tool_calls, usage) the caller's _unpack_tool_gen_result requires.
The timeout-retry-without-tools fallback returned a 2-tuple (text, []), raising
ValueError downstream and dropping usage. Guard: no tuple-return in that method has
exactly 2 elements.
"""
import ast
import inspect

from modules.llm.gemini_client import GeminiClient


def _method_source(cls, name):
    src = inspect.getsource(cls)
    tree = ast.parse(src)
    # cls source is dedented at class level; find the method node.
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def test_generate_with_tools_returns_only_3_tuples():
    method = _method_source(GeminiClient, "_generate_with_tools")
    two_tuple_returns = []
    for node in ast.walk(method):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
            if len(node.value.elts) == 2:
                two_tuple_returns.append(node.lineno)
    assert not two_tuple_returns, (
        f"_generate_with_tools has 2-tuple return(s) at {two_tuple_returns}; "
        "the contract is (text, tool_calls, usage)"
    )
