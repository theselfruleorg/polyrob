"""057 WS-B: one tool result's size on the wire, capped honestly."""
import pytest

from agents.task.agent.core.result_budget import truncate_tool_result
from agents.task.agent.core.result_processing import _pair_results_to_calls
from agents.task.agent.views import ActionResult


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("TOOL_RESULT_MAX_TOKENS", raising=False)


def test_off_by_default():
    big = "x" * 100000
    assert truncate_tool_result(big) is big


def test_non_strings_and_empties_pass_through(monkeypatch):
    monkeypatch.setenv("TOOL_RESULT_MAX_TOKENS", "10")
    assert truncate_tool_result("") == ""
    assert truncate_tool_result(None) is None


def test_within_budget_is_untouched(monkeypatch):
    monkeypatch.setenv("TOOL_RESULT_MAX_TOKENS", "100")
    small = "y" * 200
    assert truncate_tool_result(small) is small


def test_the_cut_names_the_numbers_and_the_way_out(monkeypatch):
    monkeypatch.setenv("TOOL_RESULT_MAX_TOKENS", "100")
    out = truncate_tool_result("z" * 40000)
    assert out.startswith("z" * 400)
    assert "truncated: 100 of 10,000 tokens" in out
    assert "offset/limit" in out


def test_the_action_result_itself_is_never_mutated(monkeypatch):
    monkeypatch.setenv("TOOL_RESULT_MAX_TOKENS", "10")
    body = "q" * 10000
    ar = ActionResult(extracted_content=body, tool_call_id="c1")
    paired = _pair_results_to_calls([ar], [{"id": "c1", "name": "read_file"}])
    wired = paired["c1"][0]
    assert len(wired) < len(body) and "truncated" in wired
    assert ar.extracted_content == body, "memory/telemetry keep the full content"


def test_an_error_string_is_capped_too(monkeypatch):
    monkeypatch.setenv("TOOL_RESULT_MAX_TOKENS", "10")
    ar = ActionResult(error="e" * 10000, tool_call_id="c1")
    paired = _pair_results_to_calls([ar], [{"id": "c1", "name": "web_fetch"}])
    content, had_error = paired["c1"]
    assert had_error and "truncated" in content


def test_truncation_runs_before_the_untrusted_wrap(monkeypatch):
    """A cut applied after the wrap would remove the closing delimiter."""
    monkeypatch.setenv("TOOL_RESULT_MAX_TOKENS", "50")
    ar = ActionResult(extracted_content="w" * 10000, tool_call_id="c1")
    paired = _pair_results_to_calls(
        [ar], [{"id": "c1", "name": "web_fetch"}],
        source_for=lambda _id: ("web_fetch", "web_fetch"))
    content = paired["c1"][0]
    assert "truncated" in content
    if "<untrusted_tool_result" in content:
        assert content.rstrip().endswith("</untrusted_tool_result>")
