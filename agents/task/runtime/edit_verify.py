"""Deterministic "did the agent edit code without re-running tests" check
(I-3 harness-review finding ≡ H3 HF-proposal "edit-then-finish contract",
merged under dedup decision D1).

Coding-specific by design (names ``str_replace``/``apply_patch``/``create_file``/
``move_file``/``delete_file`` and ``run_tests``) — this is the deliberate, scoped
exception to ``goals/completion_judge.py``'s capability-agnostic rule (that judge
reasons about arbitrary goal *outcomes* via an LLM; this check reasons about ONE
narrow, mechanical fact — was a code-editing action's ledger entry newer than the
last clean test run — with no LLM at all).

Uses action history and harness-owned execution receipts. Legacy sequence is
comparable within one agent only; cross-agent verification needs a matching
clock, session, workspace, and a test START after the edit FINISH. Missing
results never prove test success. Whole-ledger introspection failures remain
fail-open for compatibility; this is not an artifact-digest verification gate.
"""
from __future__ import annotations  # OK here: this is NOT an action-registration module

from typing import Any

from agents.task.runtime.evidence import walk_action_events

# Names come from tools/coding/tool.py's registered actions.
_EDIT_ACTIONS = frozenset({"str_replace", "apply_patch", "create_file", "move_file", "delete_file", "self_env_patch_source"})
_TEST_ACTIONS = frozenset({"run_tests"})

# Public contract name (R-4): external consumers (tools/hf_deploy/digest.py's
# ship==tested gate) must not bind to the private spelling.
TEST_ACTIONS = _TEST_ACTIONS


def edited_since_last_test(orchestrator: Any) -> bool:
    """True if an edit lacks a subsequent, successful test in comparable scope.

    Missing edit results are uncertain and need verification. Explicitly errored
    edits retain the legacy no-change assumption; partial-write error semantics
    and digest-bound tests require the later effect-receipt contract.
    """
    try:
        events = list(walk_action_events(orchestrator))
        edits = [event for event in events if event.name in _EDIT_ACTIONS
                 and not getattr(event.result, "error", None)]
        tests = [event for event in events if event.name in _TEST_ACTIONS
                 and event.result is not None and not getattr(event.result, "error", None)
                 and event.receipt.get("ok", True)]
        for edit in edits:
            def verifies(test):
                e, t = edit.receipt, test.receipt
                if e or t:
                    # Unknown/mixed clock domains, scopes, or missing times do
                    # not prove an edit preceded the verification's START.
                    return bool(e.get("clock_id") and e.get("clock_id") == t.get("clock_id")
                                and e.get("workspace") and e.get("workspace") == t.get("workspace")
                                and e.get("session_id") == t.get("session_id")
                                and isinstance(e.get("finished_ns"), int)
                                and isinstance(t.get("started_ns"), int)
                                and e["finished_ns"] <= t["started_ns"])
                # Legacy histories prove sequence ONLY within their own agent.
                return edit.agent_id == test.agent_id and edit.sequence < test.sequence
            if not any(verifies(test) for test in tests):
                return True
    except Exception:
        return False  # fail-open: never block a finish on an introspection miss
    return False
