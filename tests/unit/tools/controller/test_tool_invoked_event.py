"""058 T4.1 — the ``tool_invoked`` event: nothing could say which of the 36 tools
the agent reaches for. ONE helper (``_record_invocation``) is called from the
success path AND the error-observation path, so an invocation that failed is
counted too; the tool id resolves through the EXISTING ``get_action_details``
resolver; ``attrs`` is an explicit dict; ``TOOL_USAGE_TELEMETRY=false`` emits
nothing (byte-identical OFF)."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.controller import execution as ex

_REPO = Path(__file__).resolve().parents[4]


class _Ctrl(ex.ExecutionMixin):
    def __init__(self, tool="filesystem"):
        self._tool = tool
        import logging
        self.logger = logging.getLogger("t")

    def get_action_details(self, name):
        return None if self._tool is None else SimpleNamespace(tool=self._tool)


def _capture(monkeypatch):
    seen = []
    import core.event_log as el
    monkeypatch.setattr(el, "emit", lambda kind, **kw: seen.append((kind, kw)))
    return seen


def test_success_emits_tool_invoked_with_tool_action_ok_and_ms(monkeypatch):
    monkeypatch.delenv("TOOL_USAGE_TELEMETRY", raising=False)
    seen = _capture(monkeypatch)
    ctx = SimpleNamespace(user_id="u1", session_id="s1")
    result = SimpleNamespace(error=None)
    _Ctrl()._record_invocation("read_file", result, ctx, started_ns=0)
    assert len(seen) == 1
    kind, kw = seen[0]
    assert kind == "tool_invoked" and kw["source"] == "tool"
    assert kw["user_id"] == "u1" and kw["session_id"] == "s1"
    attrs = kw["attrs"]
    assert attrs["tool"] == "filesystem" and attrs["action"] == "read_file"
    assert attrs["ok"] is True and isinstance(attrs["ms"], int)


def test_error_result_is_counted_as_not_ok(monkeypatch):
    seen = _capture(monkeypatch)
    _Ctrl()._record_invocation("read_file", SimpleNamespace(error="boom"), None, started_ns=None)
    assert seen[0][1]["attrs"]["ok"] is False
    assert seen[0][1]["attrs"]["ms"] is None


def test_unresolvable_action_records_unknown_tool_not_nothing(monkeypatch):
    """An action the registry cannot map is still an invocation; 'unknown' is
    an honest bucket, silence is not."""
    seen = _capture(monkeypatch)
    _Ctrl(tool=None)._record_invocation("mystery", SimpleNamespace(error=None), None, started_ns=0)
    assert seen[0][1]["attrs"]["tool"] == "unknown"


def test_flag_off_emits_nothing(monkeypatch):
    monkeypatch.setenv("TOOL_USAGE_TELEMETRY", "false")
    seen = _capture(monkeypatch)
    _Ctrl()._record_invocation("read_file", SimpleNamespace(error=None), None, started_ns=0)
    assert seen == []


def test_a_raising_resolver_never_breaks_execution(monkeypatch):
    class _Bad(_Ctrl):
        def get_action_details(self, name):
            raise RuntimeError("registry exploded")
    seen = _capture(monkeypatch)
    _Bad()._record_invocation("x", SimpleNamespace(error=None), None, started_ns=0)  # must not raise
    assert seen and seen[0][1]["attrs"]["tool"] == "unknown"


def test_both_result_paths_call_the_one_helper():
    src = (_REPO / "tools/controller/execution.py").read_text()
    body = src[src.index("async def multi_act("):]
    success = body[:body.index("async def _observe_error_result(")]
    error = body[body.index("async def _observe_error_result("):body.index("async def act(")]
    assert success.count("self._record_invocation(") == 1, "success path records once"
    assert error.count("self._record_invocation(") == 1, "every error path funnels through _observe_error_result"
