"""Deterministic "did the agent edit code without re-running tests" check
(I-3 harness-review finding ≡ H3 HF-proposal "edit-then-finish contract",
merged under dedup decision D1).

Coding-specific by design (names the ``coding`` tool's ``str_replace``/``apply_patch``/
``create_file``/``move_file``/``delete_file`` and ``run_tests``, by their registered keys) — this is the deliberate, scoped
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
from core.action_names import action_names

# The REGISTERED keys (``coding_str_replace``), not the bare method names: the
# ledger records what the Controller registered. Matching the bare names left
# this check dead in production (coding-agent review B1, 2026-09-24).
_EDIT_ACTIONS = (action_names("coding", "str_replace", "apply_patch", "create_file",
                              "move_file", "delete_file")
                 | action_names("self_env", "self_env_patch_source"))
_TEST_ACTIONS = action_names("coding", "run_tests")

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


#: Refusals that mean "tests cannot run on THIS deploy" — a posture, not a
#: failed test. Nudging "run tests" after one of these asks for the impossible:
#: 2026-09-25 the prod agent got the nudge six times after `run_tests` had
#: already said "Do not retry" (no Docker for the agent identity, 053 pending).
_UNRUNNABLE_MARKERS = (
    "code execution unavailable on this deploy",
    "host execution refused while wallet custody",
)

#: Edits to these are prose, not code: no test run can verify them.
_PROSE_SUFFIXES = (".md", ".txt", ".rst")


#: Every path field a coding edit carries (tools/coding/tool.py): the edit verbs
#: name ``file_path``; ``move_file`` names BOTH ``src_path`` and ``dest_path``
#: (068 B13 — a move was read as an unknown path, so moving README.md into
#: docs/ still drew the "run tests" nudge).
_PATH_FIELDS = ("file_path", "path", "src_path", "dest_path", "target", "dest")


def _edit_paths(event) -> list:
    """Every path the edit touched; ``[]`` when none can be read (= code)."""
    try:
        dumped = event.action.model_dump(exclude_unset=True)
    except Exception:
        return []
    out = []
    for params in (dumped or {}).values():
        if isinstance(params, dict):
            out += [params[k] for k in _PATH_FIELDS if isinstance(params.get(k), str)]
    return out


def _is_prose_edit(event) -> bool:
    paths = _edit_paths(event)
    return bool(paths) and all(p.lower().endswith(_PROSE_SUFFIXES) for p in paths)


def verify_nudge_applies(orchestrator: Any) -> bool:
    """False when the "run tests" nudge cannot be satisfied or is meaningless.

    068 G7: the nudge is skipped when a test run this session was refused by the
    deploy's posture, or when every edit was to a prose file. Unknown paths count
    as code (the nudge stays). Fail-open to True — never suppress the nudge on an
    introspection miss, because suppressing is the permissive direction.
    """
    try:
        events = list(walk_action_events(orchestrator))
        for event in events:
            if event.name in _TEST_ACTIONS:
                err = str(getattr(event.result, "error", "") or "").lower()
                if any(marker in err for marker in _UNRUNNABLE_MARKERS):
                    return False
        edits = [event for event in events if event.name in _EDIT_ACTIONS
                 and not getattr(event.result, "error", None)]
        if edits and all(_is_prose_edit(e) for e in edits):
            return False
    except Exception:
        return True
    return True
