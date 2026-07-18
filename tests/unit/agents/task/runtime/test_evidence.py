"""§4.1 evidence pack + §4.2 deterministic invariants.

The pack is collected MECHANICALLY at run end (no LLM, no goal-type knowledge):
action ledger, workspace artifact diff (absorbs proposal 007), final-step
errors, ids/urls captured from successful tool RESULTS, and the agent's own
user messages. It rides RunOutcome.evidence into episodes and goal_events so
every downstream consumer sees the same facts.
"""
import asyncio
import os
import time
from types import SimpleNamespace

from tools.controller.types import ActionResult


class _Action:
    def __init__(self, name, params=None):
        self._d = {name: params or {}}

    def model_dump(self, exclude_unset=True):
        return dict(self._d)


class _Step:
    def __init__(self, actions, results):
        self.model_output = SimpleNamespace(action=list(actions))
        self.result = list(results)


class _Agent:
    def __init__(self, steps, *, is_sub=False, n_steps=1):
        self.history = SimpleNamespace(history=list(steps))
        self._is_sub_agent = is_sub
        self._last_result = steps[-1].result if steps else None
        self.state = SimpleNamespace(n_steps=n_steps, session_created_at=None)


def _orch(steps, session_id="sess-1"):
    return SimpleNamespace(agents={"main": _Agent(steps)}, session_id=session_id,
                           usage_tracker=None, user_id="u1")


# ---------------------------------------------------------------------------
# Ledger lines / errors / captured refs
# ---------------------------------------------------------------------------

def test_evidence_ledger_pairs_actions_with_status():
    from agents.task.runtime.evidence import build_evidence
    orch = _orch([
        _Step([_Action("filesystem_write_file", {"file_path": "a.md"})],
              [ActionResult(extracted_content="wrote a.md")]),
        _Step([_Action("x402_request")],
              [ActionResult(error="payment-request store unavailable (no database service)")]),
    ])
    pack = build_evidence(orch)
    joined = "\n".join(pack.ledger)
    assert "filesystem_write_file" in joined and "ok" in joined
    assert "x402_request" in joined and "ERROR" in joined
    assert "payment-request store unavailable" in joined


def test_evidence_errors_tail_collects_final_step_errors():
    from agents.task.runtime.evidence import build_evidence
    orch = _orch([
        _Step([_Action("browser_navigate")], [ActionResult(error="early error, scrolled past")]),
        _Step([_Action("noop")], [ActionResult(extracted_content="ok")]),
        _Step([_Action("x402_request")], [ActionResult(error="store unavailable")]),
        _Step([_Action("done")], [ActionResult(is_done=True, extracted_content="OUTCOME: BLOCKED — x")]),
    ])
    pack = build_evidence(orch)
    assert any("store unavailable" in e for e in pack.errors_tail)


def test_evidence_captures_urls_and_paths_from_successful_results_only():
    from agents.task.runtime.evidence import build_evidence
    orch = _orch([
        _Step([_Action("twitter_post")],
              [ActionResult(extracted_content="posted: https://x.com/rob/status/12345")]),
        _Step([_Action("web_fetch")],
              [ActionResult(error="404 on https://example.com/broken")]),
    ])
    pack = build_evidence(orch)
    assert "https://x.com/rob/status/12345" in pack.captured_refs
    assert not any("broken" in r for r in pack.captured_refs), \
        "refs come from SUCCESSFUL results (facts), never from errors/claims"


# ---------------------------------------------------------------------------
# Artifacts (absorbs proposal 007)
# ---------------------------------------------------------------------------

def test_artifacts_from_workspace_scan(tmp_path):
    from agents.task.runtime.evidence import collect_artifacts
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "report.md").write_text("# findings\n")
    arts = collect_artifacts(_orch([]), workspace_dir=str(ws))
    assert any(a.get("path", "").endswith("report.md") for a in arts)
    assert all("bytes" in a for a in arts if a.get("path"))


def test_artifacts_ledger_descriptors_for_real_output_actions():
    from agents.task.runtime.evidence import collect_artifacts
    orch = _orch([
        _Step([_Action("twitter_post", {"text": "hello world"})],
              [ActionResult(extracted_content="posted: https://x.com/rob/status/999")]),
        _Step([_Action("browser_navigate")],
              [ActionResult(extracted_content="navigated")]),  # not an output action
    ])
    arts = collect_artifacts(orch, workspace_dir=None)
    kinds = [a.get("kind") for a in arts]
    assert "twitter_post" in kinds
    assert "browser_navigate" not in kinds


def test_artifacts_workspace_scan_respects_time_window(tmp_path):
    from agents.task.runtime.evidence import collect_artifacts
    ws = tmp_path / "workspace"
    ws.mkdir()
    old = ws / "preexisting.txt"
    old.write_text("old")
    past = time.time() - 3600
    os.utime(old, (past, past))
    new = ws / "fresh.txt"
    new.write_text("new")
    arts = collect_artifacts(_orch([]), workspace_dir=str(ws), started_ts=time.time() - 60)
    paths = [a.get("path", "") for a in arts]
    assert any(p.endswith("fresh.txt") for p in paths)
    assert not any(p.endswith("preexisting.txt") for p in paths)


def test_artifacts_fail_open_on_bad_dir():
    from agents.task.runtime.evidence import collect_artifacts
    assert collect_artifacts(_orch([]), workspace_dir="/nonexistent/nope") == []


# ---------------------------------------------------------------------------
# §4.2 invariant: done() where EVERY substantive action errored → failure
# ---------------------------------------------------------------------------

def test_all_actions_errored_true_when_only_errors():
    from agents.task.runtime.run_outcome import all_actions_errored
    orch = _orch([
        _Step([_Action("x402_request")], [ActionResult(error="store unavailable")]),
        _Step([_Action("filesystem_write_file")], [ActionResult(error="permission denied")]),
        _Step([_Action("done")], [ActionResult(is_done=True, extracted_content="all done!")]),
    ])
    assert all_actions_errored(orch) is True


def test_all_actions_errored_false_when_any_success():
    from agents.task.runtime.run_outcome import all_actions_errored
    orch = _orch([
        _Step([_Action("x402_request")], [ActionResult(error="store unavailable")]),
        _Step([_Action("filesystem_write_file")], [ActionResult(extracted_content="wrote a.md")]),
        _Step([_Action("done")], [ActionResult(is_done=True, extracted_content="done")]),
    ])
    assert all_actions_errored(orch) is False


def test_all_actions_errored_false_when_no_substantive_actions():
    """done/send_message are communication, not work — a pure-chat run must not
    trip the invariant."""
    from agents.task.runtime.run_outcome import all_actions_errored
    orch = _orch([
        _Step([_Action("send_message", {"text": "hi"})],
              [ActionResult(extracted_content="Message sent to user (non-blocking)")]),
        _Step([_Action("done")], [ActionResult(is_done=True, extracted_content="done")]),
    ])
    assert all_actions_errored(orch) is False


def test_build_run_outcome_populates_evidence_and_artifacts(tmp_path):
    from agents.task.runtime.run_outcome import build_run_outcome
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "out.md").write_text("deliverable")
    orch = _orch([
        _Step([_Action("filesystem_write_file", {"file_path": "out.md"})],
              [ActionResult(extracted_content="wrote out.md")]),
        _Step([_Action("done", {"text": "OUTCOME: workspace/out.md"})],
              [ActionResult(is_done=True, extracted_content="OUTCOME: workspace/out.md")]),
    ])

    class _TA:
        def get_orchestrator(self, sid):
            return orch

        def _extract_chat_reply(self, sid):
            return ""

    import agents.task.runtime.evidence as ev_mod
    orig = ev_mod._resolve_workspace_dir
    ev_mod._resolve_workspace_dir = lambda o: str(ws)
    try:
        o = asyncio.run(build_run_outcome(_TA(), "sess-1", "Session completed successfully"))
    finally:
        ev_mod._resolve_workspace_dir = orig
    assert o.evidence is not None
    assert any("filesystem_write_file" in l for l in o.evidence.ledger)
    assert any(a.get("path", "").endswith("out.md") for a in o.artifacts)
