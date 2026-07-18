"""§4.4 typed acceptance checks — optional sharpener, never a gate.

Framework-executed probes a producer MAY set (operator seed, eval harness, or
the agent authoring checks for its own goals). When present they run
fail-CLOSED and their results join the evidence pack. Nothing rejects a goal
without them.
"""
import asyncio

import pytest


def _run(checks, **kw):
    from agents.task.runtime.acceptance_checks import run_acceptance_checks
    return asyncio.run(run_acceptance_checks(checks, **kw))


def test_artifact_glob_pass_and_fail(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "report.md").write_text("# done")
    results = _run([{"type": "artifact_glob", "pattern": "*.md"}],
                   workspace_dir=str(ws))
    assert results[0]["ok"] is True
    results = _run([{"type": "artifact_glob", "pattern": "*.pdf"}],
                   workspace_dir=str(ws))
    assert results[0]["ok"] is False


def test_http_ok_uses_status(monkeypatch):
    import agents.task.runtime.acceptance_checks as ac
    monkeypatch.setattr(ac, "_http_status", lambda url, timeout: 200)
    assert _run([{"type": "http_ok", "url": "https://example.com/x"}])[0]["ok"] is True
    monkeypatch.setattr(ac, "_http_status", lambda url, timeout: 500)
    assert _run([{"type": "http_ok", "url": "https://example.com/x"}])[0]["ok"] is False


# --- file_contains (proposal 016 #1) --------------------------------------------

def test_file_contains_all_substrings_pass(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "report.md").write_text("# Testing\n...\n# Deployment\n")
    results = _run([{"type": "file_contains", "path": "report.md",
                     "contains": ["Testing", "Deployment"]}],
                   workspace_dir=str(ws))
    assert results[0]["ok"] is True
    assert "2/2" in results[0]["detail"]


def test_file_contains_missing_substring_fails_with_detail(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "report.md").write_text("# Testing only\n")
    results = _run([{"type": "file_contains", "path": "report.md",
                     "contains": ["Testing", "Deployment"]}],
                   workspace_dir=str(ws))
    assert results[0]["ok"] is False
    assert "missing" in results[0]["detail"].lower()
    assert "Deployment" in results[0]["detail"]


def test_file_contains_missing_file_fails(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    results = _run([{"type": "file_contains", "path": "nope.md",
                     "contains": ["anything"]}],
                   workspace_dir=str(ws))
    assert results[0]["ok"] is False
    assert "not found" in results[0]["detail"].lower()


def test_file_contains_oversized_file_fails_gracefully(tmp_path, monkeypatch):
    import agents.task.runtime.acceptance_checks as ac
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "big.md").write_text("Testing " * 100)
    monkeypatch.setattr(ac, "FILE_CONTAINS_MAX_BYTES", 16)  # avoid a real 1MB fixture
    results = _run([{"type": "file_contains", "path": "big.md",
                     "contains": ["Testing"]}],
                   workspace_dir=str(ws))
    assert results[0]["ok"] is False
    assert "too large" in results[0]["detail"].lower()


def test_file_contains_mode_any(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "report.md").write_text("# Deployment notes\n")
    # any: one of two present -> pass
    results = _run([{"type": "file_contains", "path": "report.md",
                     "contains": ["Testing", "Deployment"], "mode": "any"}],
                   workspace_dir=str(ws))
    assert results[0]["ok"] is True
    # any: none present -> fail
    results = _run([{"type": "file_contains", "path": "report.md",
                     "contains": ["Alpha", "Beta"], "mode": "any"}],
                   workspace_dir=str(ws))
    assert results[0]["ok"] is False
    # default (unset mode) stays strict: same file misses 'Testing' -> fail
    results = _run([{"type": "file_contains", "path": "report.md",
                     "contains": ["Testing", "Deployment"]}],
                   workspace_dir=str(ws))
    assert results[0]["ok"] is False


def test_file_contains_no_workspace_or_empty_contains_fail(tmp_path):
    results = _run([{"type": "file_contains", "path": "x.md", "contains": ["A"]}])
    assert results[0]["ok"] is False
    assert "workspace" in results[0]["detail"].lower()
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "x.md").write_text("A")
    results = _run([{"type": "file_contains", "path": "x.md", "contains": []}],
                   workspace_dir=str(ws))
    assert results[0]["ok"] is False


def test_unknown_check_type_fails_closed():
    results = _run([{"type": "wallet_delta", "min": 1}])
    assert results[0]["ok"] is False
    assert "unknown" in results[0]["detail"].lower()


def test_instance_registered_extension_runs():
    """Instance verticals (invoice/wallet-delta) live OUTSIDE core, registered
    the way tools are."""
    import agents.task.runtime.acceptance_checks as ac

    async def _always_ok(check, ctx):
        return True, "vertical says yes"

    ac.register_check_type("invoice_row", _always_ok)
    try:
        results = _run([{"type": "invoice_row", "id": "abc"}])
        assert results[0]["ok"] is True
    finally:
        ac._CHECK_TYPES.pop("invoice_row", None)


def test_check_crash_is_a_failed_check_not_an_exception():
    import agents.task.runtime.acceptance_checks as ac

    async def _boom(check, ctx):
        raise RuntimeError("verifier exploded")

    ac.register_check_type("boom", _boom)
    try:
        results = _run([{"type": "boom"}])
        assert results[0]["ok"] is False
    finally:
        ac._CHECK_TYPES.pop("boom", None)


# --- dispatcher wiring: present checks run fail-closed --------------------------

def test_dispatcher_fails_run_on_failed_check(tmp_path):
    from types import SimpleNamespace
    from agents.task.goals.board import Goal, STATUS_READY
    from agents.task.goals.dispatcher import GoalDispatcher
    from tools.controller.types import ActionResult

    class _Board:
        def __init__(self):
            self.failures, self.successes = [], []

        def record_failure(self, gid, error=None, session_id=None):
            self.failures.append((gid, error))
            return Goal(id=gid, user_id="u1", title="t", status=STATUS_READY)

        def record_success(self, gid, session_id=None, result=None):
            self.successes.append(gid)

        def create_ask(self, **kw):
            return None

        def get(self, gid):
            return Goal(id=gid, user_id="u1", title="t", status="done")

        def set_outcome(self, gid, outcome):
            return True

        def block_from_ready(self, gid, *, error):
            return True

    done = ActionResult(is_done=True, extracted_content="Saved the report!\nOUTCOME: report.md")
    agent_stub = SimpleNamespace(
        history=SimpleNamespace(history=[SimpleNamespace(
            model_output=SimpleNamespace(action=[]), result=[done])]),
        _is_sub_agent=False, _last_result=[done],
        state=SimpleNamespace(n_steps=2, session_created_at=None))
    orch = SimpleNamespace(agents={"m": agent_stub}, session_id="s1", usage_tracker=None)

    class _TA:
        async def create_session(self, *, user_id, request):
            return {"id": "s1"}

        async def run_session(self, user_id, session_id):
            return "Session completed successfully"

        def get_orchestrator(self, sid):
            return orch

        def _extract_chat_reply(self, sid):
            return ""

        deliver_self_wake = None

    board = _Board()
    d = GoalDispatcher(board, _TA())
    goal = Goal(id="g1", user_id="u1", title="write report", payload={
        "acceptance_checks": [{"type": "artifact_glob", "pattern": "*.pdf",
                               "workspace_dir": str(tmp_path)}],
    })
    asyncio.run(d._run_goal(goal))
    assert not board.successes, "a failed typed check must fail the run (fail-closed)"
    assert board.failures and "acceptance check" in board.failures[0][1].lower()
