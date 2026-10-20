"""058 T4.1 — the ``tool_invoked`` event: nothing could say which of the 36 tools
the agent reaches for. ONE helper (``_record_invocation``) is called from the
success path AND the error-observation path, so an invocation that failed is
counted too; the tool id resolves through the EXISTING ``get_action_details``
resolver; ``attrs`` is an explicit dict."""
from pathlib import Path
from types import SimpleNamespace

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


# --- D-9 (review 2026-10-14): model + error_class on every row ---------------

def test_row_carries_model_from_context_metadata_and_no_error_class_on_success(monkeypatch):
    seen = _capture(monkeypatch)
    ctx = SimpleNamespace(user_id="u1", session_id="s1", metadata={"model": "z-ai/glm-5.3"})
    _Ctrl()._record_invocation("read_file", SimpleNamespace(error=None), ctx, started_ns=0)
    attrs = seen[0][1]["attrs"]
    assert attrs["model"] == "z-ai/glm-5.3" and attrs["error_class"] is None


def test_model_falls_back_to_the_orchestrators_agent(monkeypatch):
    seen = _capture(monkeypatch)
    ctrl = _Ctrl()
    agent = SimpleNamespace(llm=SimpleNamespace(model_name="deepseek/deepseek-v4.1-flash"))
    ctrl.orchestrator = SimpleNamespace(agents={"a1": agent})
    ctx = SimpleNamespace(user_id="u1", session_id="s1", agent_id="a1", metadata={})
    ctrl._record_invocation("read_file", SimpleNamespace(error="Action 1/1 (x) timed out after 30 seconds"), ctx)
    attrs = seen[0][1]["attrs"]
    assert attrs["model"] == "deepseek/deepseek-v4.1-flash"
    assert attrs["error_class"] == "timeout"


def test_no_model_source_records_none_not_a_guess(monkeypatch):
    seen = _capture(monkeypatch)
    _Ctrl()._record_invocation("x", SimpleNamespace(error=None), None, started_ns=0)
    assert seen[0][1]["attrs"]["model"] is None


def test_error_classes():
    from tools.controller.tool_error_class import classify_tool_error as c
    assert c(None) is None and c("") is None
    assert c("Action 2/3 (shell_run) timed out after 60 seconds") == "timeout"
    assert c("Error executing action write_file: 1 validation error for WriteFileAction") == "validation"
    assert c("refused: this input matches the owner's shell deny list") == "refused"
    assert c("Error executing action read_file: [Errno 2] No such file or directory: 'a'") == "not_found"
    assert c("Error in action 1/1: Error code: 503 service unavailable") == "provider"
    assert c("Error in action 1/1: division by zero") == "other"
