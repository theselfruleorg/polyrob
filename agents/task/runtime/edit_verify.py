"""Deterministic "did the agent edit code without re-running tests" check
(I-3 harness-review finding ≡ H3 HF-proposal "edit-then-finish contract",
merged under dedup decision D1).

Coding-specific by design (names ``str_replace``/``apply_patch``/``create_file``/
``move_file``/``delete_file`` and ``run_tests``) — this is the deliberate, scoped
exception to ``goals/completion_judge.py``'s capability-agnostic rule (that judge
reasons about arbitrary goal *outcomes* via an LLM; this check reasons about ONE
narrow, mechanical fact — was a code-editing action's ledger entry newer than the
last clean test run — with no LLM at all).

Derives the signal entirely from the EXISTING action ledger
(``runtime/evidence.py::_walk_ledger`` — the same walker ``build_evidence`` and
``goals/completion_judge.py``'s evidence builder already read) rather than adding
new session-timestamp state: no ``last_edit_ts``/``last_green_test_ts``, no
workspace-digest stamping. Deterministic, bounded, fail-open — a ledger-walk
error (or no orchestrator at all) must never block a finish.
"""
from __future__ import annotations  # OK here: this is NOT an action-registration module

from typing import Any

from agents.task.runtime.evidence import _walk_ledger

# Names come from tools/coding/tool.py's registered actions.
_EDIT_ACTIONS = frozenset({"str_replace", "apply_patch", "create_file", "move_file", "delete_file"})
_TEST_ACTIONS = frozenset({"run_tests"})

# Public contract name (R-4): external consumers (tools/hf_deploy/digest.py's
# ship==tested gate) must not bind to the private spelling.
TEST_ACTIONS = _TEST_ACTIONS


def edited_since_last_test(orchestrator: Any) -> bool:
    """True when the ledger shows a successful code-edit action more recently
    than the last successful ``run_tests`` — or when there was never a
    successful ``run_tests`` at all but at least one successful edit happened.

    Whole-session ledger scope (v1): walks every step of every agent
    (`_walk_ledger` already labels sub-agent steps, but this check does not
    special-case them — an edit made by a delegated sub-agent still leaves the
    session unverified). Only SUCCESSFUL actions move the watermarks: an
    errored edit didn't really change anything, and ``tools/coding/tool.py``'s
    ``run_tests`` returns an error result on a non-zero exit (failing suite),
    not just on a framework-level crash — so an errored ``run_tests`` entry
    correctly does NOT count as "tests are green".
    """
    last_edit = last_test = -1
    try:
        for i, (_label, name, _action, result) in enumerate(_walk_ledger(orchestrator)):
            if getattr(result, "error", None):
                continue
            if name in _EDIT_ACTIONS:
                last_edit = i
            elif name in _TEST_ACTIONS:
                last_test = i
    except Exception:
        return False  # fail-open: never block a finish on an introspection miss
    return last_edit > last_test
