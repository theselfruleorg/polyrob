"""Regression: the non-MCP retry circuit-breaker key must discriminate by params.

Keying by action name alone let a burst of failures against bad targets exhaust
the shared counter and then reject a later VALID call to the same action.
"""
from tools.controller.execution import ExecutionMixin


def _key(name, params):
    m = ExecutionMixin.__new__(ExecutionMixin)
    return m._get_operation_key(name, params)


def test_distinct_params_get_distinct_keys():
    assert _key("filesystem_write_file", {"file_path": "/a"}) != \
        _key("filesystem_write_file", {"file_path": "/b"})


def test_stable_for_same_params():
    assert _key("filesystem_write_file", {"file_path": "/a"}) == \
        _key("filesystem_write_file", {"file_path": "/a"})


def test_mcp_key_unchanged():
    assert _key("mcp_execute_tool", {"server_name": "s", "tool_name": "t"}) == "mcp:s:t"


def test_pydantic_model_params_supported():
    class _P:
        def model_dump(self):
            return {"file_path": "/x"}

    k = _key("coding_str_replace", _P())
    assert k.startswith("coding_str_replace:")
