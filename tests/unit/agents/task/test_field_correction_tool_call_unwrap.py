"""2026-11-22 16:31Z prod (session 028cdf47): the model emitted the meta-verb
``tool_call`` with its payload wrapped ONE level too deep —
``{'arguments': {'name': 'anysite_api', 'arguments': {...}}}`` — so
``ToolCallAction`` validation failed with ``Missing required: ['name']`` and the
run dropped three calls before recovering by re-emitting them. The corrections
layer unwraps that exact shape: the outer dict holding ONLY ``arguments``,
whose value is a dict carrying ``name``.
"""
from agents.task.utils_json import apply_action_field_corrections as fix


def test_tool_call_payload_wrapped_in_arguments_is_unwrapped():
    out = fix("tool_call", {"arguments": {"name": "anysite_api", "arguments": {
        "endpoint": "/api/twitter/posts/replies",
        "params": {"post": "https://x.com/example/status/1", "count": 40}}}})
    assert out == {"name": "anysite_api", "arguments": {
        "endpoint": "/api/twitter/posts/replies",
        "params": {"post": "https://x.com/example/status/1", "count": 40}}}


def test_a_top_level_name_is_never_clobbered():
    """If the model ALSO named the tool at the top level, the real payload
    stands — the unwrap only rescues the wrapped-only shape."""
    out = fix("tool_call", {"name": "anysite_api", "arguments": {
        "name": "anysite_api", "arguments": {"endpoint": "/e"}}})
    assert out["name"] == "anysite_api"
    assert out["arguments"] == {"name": "anysite_api", "arguments": {"endpoint": "/e"}}


def test_a_scalar_or_nameless_inner_arguments_is_untouched():
    """Only a dict-carrying-name inner payload unwraps; anything else keeps the
    original params (a legit ``arguments`` string arg, say)."""
    for bad in ({"arguments": "just a string"},
                {"arguments": {"endpoint": "/e"}},   # inner dict without 'name'
                {"arguments": {"name": 7, "x": 1}},  # 'name' not a string
                {"arguments": {"name": "t"}, "extra": 1}):  # outer not wrapper-only
        assert fix("tool_call", bad) == bad
