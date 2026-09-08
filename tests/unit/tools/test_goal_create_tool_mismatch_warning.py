"""2026-08-18 (intel finding, MEDIUM-HIGH — recurred across x402/video/micro-app goal
families): a self-created goal with an explicit `tools` list AND an `http_ok`
acceptance check is structurally doomed. `dispatcher._resolve_goal_tools` returns
`payload.tools` verbatim once it's non-empty (no widening for compute posture), and
shell/process/code_execution can NEVER be added via goal_create at all — they're
deliberately excluded from the self-goal allowlist (a security boundary: host/compute
tools ride AGENT_COMPUTE_POSTURE only). Live evidence: `aa39f150c773` (video render,
no shell/code_execution — Remotion needs the compute toolchain), `acf2a2a0257c`
(status-page micro-app, `coding` but no shell/process to keep its own http_ok-checked
server alive) — both failed twice then blocked.

Since nothing can be auto-appended (the missing tools are exactly the ones this path
can never grant), the fix is a loud, actionable warning at create time instead of a
silent doomed goal.
"""
import asyncio

from agents.task.goals.board import GoalBoard
from tools.goal_tools import GoalTool, GoalCreateAction, _compute_tool_mismatch_warning


class _Ctx:
    user_id = "tester"


def _make_tool(tmp_path):
    tool = GoalTool.__new__(GoalTool)
    tool._goal_board = GoalBoard(str(tmp_path / "goals.db"))
    return tool


_HTTP_OK_CHECK = [{"type": "http_ok", "url": "http://localhost:8080/healthz"}]


# --- pure helper -------------------------------------------------------------

def test_no_warning_when_tools_unset():
    assert _compute_tool_mismatch_warning(None, _HTTP_OK_CHECK) is None
    assert _compute_tool_mismatch_warning([], _HTTP_OK_CHECK) is None


def test_no_warning_when_no_http_ok_check():
    assert _compute_tool_mismatch_warning(["coding", "filesystem"], None) is None
    assert _compute_tool_mismatch_warning(
        ["coding"], [{"type": "artifact_glob", "pattern": "*.md"}]
    ) is None


def test_no_warning_when_compute_tool_already_present():
    for compute_tool in ("shell", "process", "code_execution"):
        assert _compute_tool_mismatch_warning([compute_tool, "coding"], _HTTP_OK_CHECK) is None


def test_warning_fires_on_the_exact_doomed_combination():
    warning = _compute_tool_mismatch_warning(["coding", "filesystem"], _HTTP_OK_CHECK)
    assert warning is not None
    assert "http_ok" in warning
    assert "shell" in warning and "process" in warning and "code_execution" in warning
    assert "AGENT_COMPUTE_POSTURE" in warning
    assert "omit" in warning.lower()


def test_malformed_acceptance_checks_entries_do_not_crash():
    assert _compute_tool_mismatch_warning(["coding"], ["not-a-dict", 42, None]) is None


# --- goal_create integration ---------------------------------------------------

def test_goal_create_surfaces_warning_for_doomed_combination(tmp_path):
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(
            title="status page live mile", body="serve on loopback, curl it",
            tools=["coding", "filesystem"],
            acceptance_checks=_HTTP_OK_CHECK,
        ),
        _Ctx(),
    ))
    assert res.error is None  # warns, does not block creation
    assert "⚠️" in res.extracted_content
    assert "AGENT_COMPUTE_POSTURE" in res.extracted_content


def test_goal_create_no_warning_when_tools_omitted(tmp_path, monkeypatch):
    """The doctrine-correct path (omit tools -> stays TOOLS-LESS under S1 progressive
    disclosure -> wide default + compute tools at dispatch) must stay silent — this
    is the recommended shape, not an error condition. (Prod runs with
    TOOL_PROGRESSIVE_DISCLOSURE=true; without it the legacy from-text inference
    still narrows payload.tools, which correctly ALSO warns — see the next test.)"""
    monkeypatch.setenv("TOOL_PROGRESSIVE_DISCLOSURE", "true")
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(
            title="status page live mile 2", body="serve on loopback, curl it",
            acceptance_checks=_HTTP_OK_CHECK,
        ),
        _Ctx(),
    ))
    assert "tools=" not in res.extracted_content  # S4: stays tools-less
    assert "⚠️" not in res.extracted_content


def test_goal_create_legacy_inference_also_narrows_and_warns(tmp_path, monkeypatch):
    """Without progressive disclosure, from-text inference ALSO writes a narrow
    payload.tools (pre-existing behavior, not something this fix changes) — the new
    warning correctly extends protection to this path too, not just explicit lists."""
    monkeypatch.setenv("TOOL_PROGRESSIVE_DISCLOSURE", "false")
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(
            title="status page live mile 3", body="serve on loopback, curl it",
            acceptance_checks=_HTTP_OK_CHECK,
        ),
        _Ctx(),
    ))
    assert "tools=" in res.extracted_content  # legacy inference did narrow it
    assert "⚠️" in res.extracted_content


def test_goal_create_no_warning_for_unrelated_goals(tmp_path):
    tool = _make_tool(tmp_path)
    res = asyncio.run(tool.goal_create(
        GoalCreateAction(title="write a blog post", body="b", tools=["coding", "twitter"]),
        _Ctx(),
    ))
    assert "⚠️" not in res.extracted_content
