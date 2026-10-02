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

from agents.task.agent.core.result_offload import ToolResultOffloadMixin
from tools.controller.types import ActionResult


def _agent(controller=None):
    a = ToolResultOffloadMixin.__new__(ToolResultOffloadMixin)
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


# ---------------------------------------------------------------------------
# F26: the offload thresholds — per result AND per turn
# ---------------------------------------------------------------------------


def _pm_into(tmp_path, monkeypatch):
    """Point pm().create_file_path at tmp_path and record every file written."""
    from agents.task import path as path_mod

    written = []

    class _PM:
        def create_file_path(self, session_id, subdir, filename, user_id=None):
            p = tmp_path / filename
            written.append(p)
            return p

    monkeypatch.setattr(path_mod, "pm", lambda: _PM())
    return written


def test_defaults_are_the_reference_numbers():
    from agents.task.robust_parse_config import RobustParseConfig
    assert RobustParseConfig.MAX_EXTRACTED_CONTENT_SIZE == 100_000
    assert RobustParseConfig.MAX_EXTRACTED_CONTENT_TURN_SIZE == 200_000


def test_120k_result_is_offloaded_at_the_default(tmp_path, monkeypatch):
    """The old 500K threshold left a ~30K-token result in the context."""
    written = _pm_into(tmp_path, monkeypatch)

    a = _agent(None)
    r = ActionResult(extracted_content="q" * 120_000)
    a._handle_large_action_results([r])

    assert len(written) == 1
    assert "[LARGE CONTENT STORED]" in r.extracted_content
    assert "read_file" in r.extracted_content
    assert len(Path(written[0]).read_text(encoding="utf-8")) == 120_000


def test_90k_result_alone_stays_in_context(tmp_path, monkeypatch):
    written = _pm_into(tmp_path, monkeypatch)

    a = _agent(None)
    r = ActionResult(extracted_content="q" * 90_000)
    a._handle_large_action_results([r])

    assert written == []
    assert r.extracted_content == "q" * 90_000


def test_three_80k_results_trip_the_turn_aggregate_largest_first(tmp_path, monkeypatch):
    """Each passes the per-result test; 240K chars together blow the turn budget."""
    written = _pm_into(tmp_path, monkeypatch)

    a = _agent(None)
    small = ActionResult(extracted_content="a" * 80_000)
    biggest = ActionResult(extracted_content="b" * 82_000)
    middle = ActionResult(extracted_content="c" * 81_000)
    results = [small, biggest, middle]

    a._handle_large_action_results(results)

    # The LARGEST went to disk first, and one offload was enough (240K - 82K + preview
    # is under the 200K turn cap).
    assert len(written) == 1
    assert "[LARGE CONTENT STORED]" in biggest.extracted_content
    assert small.extracted_content == "a" * 80_000
    assert middle.extracted_content == "c" * 81_000
    assert Path(written[0]).read_text(encoding="utf-8").startswith("bbb")

    total = sum(len(r.extracted_content) for r in results)
    from agents.task.robust_parse_config import RobustParseConfig
    assert total <= RobustParseConfig.MAX_EXTRACTED_CONTENT_TURN_SIZE


def test_turn_aggregate_offloads_more_than_one_when_needed(tmp_path, monkeypatch):
    written = _pm_into(tmp_path, monkeypatch)

    a = _agent(None)
    results = [ActionResult(extracted_content=chr(97 + i) * 90_000) for i in range(4)]
    a._handle_large_action_results(results)

    assert len(written) >= 2
    from agents.task.robust_parse_config import RobustParseConfig
    total = sum(len(r.extracted_content) for r in results)
    assert total <= RobustParseConfig.MAX_EXTRACTED_CONTENT_TURN_SIZE


def test_turn_total_under_budget_offloads_nothing(tmp_path, monkeypatch):
    written = _pm_into(tmp_path, monkeypatch)

    a = _agent(None)
    results = [ActionResult(extracted_content="z" * 50_000) for _ in range(3)]
    a._handle_large_action_results(results)

    assert written == []


def test_zero_disables_both_checks(tmp_path, monkeypatch):
    from agents.task.robust_parse_config import RobustParseConfig
    written = _pm_into(tmp_path, monkeypatch)
    monkeypatch.setattr(RobustParseConfig, "MAX_EXTRACTED_CONTENT_SIZE", 0)
    monkeypatch.setattr(RobustParseConfig, "MAX_EXTRACTED_CONTENT_TURN_SIZE", 0)

    a = _agent(None)
    r = ActionResult(extracted_content="q" * 900_000)
    a._handle_large_action_results([r])

    assert written == []
    assert len(r.extracted_content) == 900_000


def test_a_tiny_result_is_never_traded_for_a_pointer(tmp_path, monkeypatch):
    """Below the preview length, an offload costs more context than it saves."""
    from agents.task.robust_parse_config import RobustParseConfig
    written = _pm_into(tmp_path, monkeypatch)
    monkeypatch.setattr(RobustParseConfig, "MAX_EXTRACTED_CONTENT_TURN_SIZE", 1_000)

    a = _agent(None)
    results = [ActionResult(extracted_content="t" * 500) for _ in range(10)]
    a._handle_large_action_results(results)

    assert written == []
    assert all(len(r.extracted_content) == 500 for r in results)
