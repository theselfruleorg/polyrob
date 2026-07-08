"""P1-2 (intelligence-polish plan 2026-07-07): the large-result file offload must
frame untrusted content as DATA at write time.

Content >MAX_EXTRACTED_CONTENT_SIZE is written to a workspace file and replaced by a
pointer. The pointer is UP-06-wrapped downstream, but the FILE was written raw — so a
later `read_file` (a trusted tool, not wrapped on read) re-entered untrusted web/MCP
content unwrapped. The fix wraps the file content when the producing tool is untrusted.

NOTE: ActionResult (extra='forbid') carries no action-name field, so the reliable
untrusted signal on a result is a URL in its metadata (the web-fetch offload case the
review describes). The tool-name path in _result_is_untrusted is defensive for any
result object that DOES carry a name.
"""
import logging
from pathlib import Path

from agents.task.agent.core.memory_writer import MemoryWriterMixin
from tools.controller.types import ActionResult


def _agent(controller=None):
    a = MemoryWriterMixin.__new__(MemoryWriterMixin)
    a.session_id = "s1"
    a.user_id = "u1"
    a.controller = controller
    a.logger = logging.getLogger("p12")
    return a


class _NamedResult:
    """Minimal duck-typed result that DOES carry an action name (defensive path)."""
    def __init__(self, action_name, content, metadata=None):
        self.action_name = action_name
        self.extracted_content = content
        self.metadata = metadata or {}


class _Details:
    def __init__(self, tool):
        self.tool = tool


class _Controller:
    def __init__(self, mapping):
        self._m = mapping

    def get_action_details(self, name):
        tool = self._m.get(name)
        return _Details(tool) if tool else None


def test_result_is_untrusted_by_tool_name():
    a = _agent(_Controller({"fetch_url": "web_fetch"}))
    r = _NamedResult("fetch_url", "x")
    assert a._result_is_untrusted(r) is True


def test_result_is_untrusted_by_url_metadata():
    a = _agent(None)
    r = ActionResult(extracted_content="x", metadata={"url": "https://evil.example"})
    assert a._result_is_untrusted(r) is True


def test_result_trusted_by_default():
    a = _agent(_Controller({"write_file": "filesystem"}))
    r = ActionResult(extracted_content="x")  # no url metadata, no untrusted name
    assert a._result_is_untrusted(r) is False


def test_offloaded_untrusted_file_is_wrapped(tmp_path, monkeypatch):
    from agents.task import path as path_mod

    written = {}

    class _PM:
        def create_file_path(self, session_id, subdir, filename, user_id=None):
            p = tmp_path / filename
            written["path"] = p
            return p

    monkeypatch.setattr(path_mod, "pm", lambda: _PM())

    a = _agent(None)
    big = "SECRET_INJECTED_INSTRUCTION " + ("z" * 600_000)
    r = ActionResult(extracted_content=big, metadata={"url": "https://evil.example"})

    a._handle_large_action_results([r])

    assert "[LARGE CONTENT STORED]" in r.extracted_content
    body = Path(written["path"]).read_text(encoding="utf-8")
    assert "untrusted_tool_result" in body
    assert "SECRET_INJECTED_INSTRUCTION" in body


def test_offloaded_trusted_file_not_wrapped(tmp_path, monkeypatch):
    from agents.task import path as path_mod

    written = {}

    class _PM:
        def create_file_path(self, session_id, subdir, filename, user_id=None):
            p = tmp_path / filename
            written["path"] = p
            return p

    monkeypatch.setattr(path_mod, "pm", lambda: _PM())

    a = _agent(None)
    big = "my own generated report " + ("z" * 600_000)
    r = ActionResult(extracted_content=big)  # no url metadata → trusted

    a._handle_large_action_results([r])
    body = Path(written["path"]).read_text(encoding="utf-8")
    assert "untrusted_tool_result" not in body
