"""Pure builders for goal-run prompts and outcome parsing (unit-testable, no I/O).

Exception: `stamp_artifact_references` below DOES touch the filesystem (it stats
candidate paths to say whether they exist) — that's the one deliberate, isolated
break of this module's "no I/O" promise; every other function here stays pure.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from agents.task.goals.board import Goal
# Canonical home moved to the runtime layer (§2/§5.0 — goals consume runtime
# contracts, never the reverse). Re-exported here for existing callers.
from agents.task.runtime.run_outcome import (  # noqa: F401
    extract_outcome_line,
    parse_blocked_outcome,
)

# T9: recall-vs-filesystem honesty. Goal/planner prompts quote titles, bodies,
# acceptance criteria and past failure text verbatim — none of which is checked
# against what's ACTUALLY on disk, so a goal can confidently retry work against
# a file that was never written (or declare BLOCKED against one that now exists
# after an owner fix). Path-shaped token: starts with a word char (never a bare
# "/" or ".."), then any run of word/dot/slash/hyphen chars, ending in a known
# extension — deliberately relative-shaped so a literal absolute path or a
# leading ".." prefix can never even enter the match as written.
#
# 2026 review (T9 Important findings, both closed by the same tightening):
#   - A leading "/" or bare ".." was silently excluded by the "starts with \w"
#     rule, but the TAIL after the separator still matched on its own (e.g.
#     "/etc/passwd.txt" -> "etc/passwd.txt", "../../escape.txt" -> "escape.txt"
#     read against the workspace root) — stamping a fragment as if it were the
#     whole reference, including a false "[present, N bytes]" beside what reads
#     as an out-of-root traversal. Fixed with a negative lookbehind
#     `(?<![\w./:\\-])` immediately before the pattern: a match can never START
#     right after a path/URL separator (`/`, `.`, `:`, `-`, `\`) or a word char,
#     so a mid-token/mid-path tail can no longer be carved out and matched on
#     its own. The containment check (resolve + prefix-check against
#     ``root_real``) stays as a second, independent layer.
#   - The same shape matches URL tails ("https://example.io/spec/a.md") and
#     version-ish tokens ("node-18.2.json"), which are path-shaped but are
#     NEVER workspace artifacts — stamping them "[MISSING on disk]" is pure
#     noise dressed up as a filesystem fact, and goal/planner text is full of
#     URLs. Fixed with an asymmetric stamping rule (see below): "MISSING" is
#     only ever asserted for a token that contains "/" (a genuine directory
#     reference like ``drafts/DESIGN.md``); a bare filename-shaped token
#     (``notes.md``, ``node-18.2.json``) is stamped ONLY when it positively
#     exists in the workspace root, and is otherwise left completely
#     untouched — a MISSING claim must never be manufactured from something
#     that was never a path reference to begin with.
_ARTIFACT_PATH_RE = re.compile(r"(?<![\w./:\\-])\b[\w][\w./-]*\.(?:md|txt|py|html|json|csv)\b")


def stamp_artifact_references(text: str, workspace_root: Optional[Path] = None) -> str:
    """Append an existence marker after each workspace-relative path-shaped token.

    ` [present, N bytes]` when the resolved path is a real file under
    ``workspace_root``. ` [MISSING on disk]` is asserted ONLY for a token that
    contains a `/` — i.e. a genuine directory-path reference (``drafts/
    DESIGN.md``) — because a bare filename-shaped token is indistinguishable
    from a URL tail or a version/pin string (``node-18.2.json``) and must
    never be reported missing on the strength of that shape alone; a
    non-existent bare token is left completely untouched instead. A candidate
    that resolves outside ``workspace_root`` once symlinks/`..` are followed
    (traversal, symlink escape) is likewise left COMPLETELY UNSTAMPED rather
    than guessed at — and, independently, the match itself can never START
    right after a path/URL separator (`/`, `.`, `:`, `-`, `\\`) or a word char,
    so an absolute path (``/etc/passwd.txt``) or a `..`-prefixed traversal
    reference (``../../escape.txt``) can never have its tail carved out and
    stamped as a standalone in-root token. Never raises: any error (missing/bad
    root, unreadable text, path errors) returns ``text`` unchanged.
    ``workspace_root=None`` is a no-op (byte-identical).
    """
    if not text or not workspace_root:
        return text
    try:
        # os.fspath (not str()) so a caller passing something that isn't a real
        # path (e.g. a stray object) is rejected as an error, not silently
        # stringified into a nonsense "root" that would defeat the containment
        # check below.
        root_real = os.path.realpath(os.fspath(workspace_root))
    except Exception:
        return text

    def _stamp(match: "re.Match") -> str:
        candidate = match.group(0)
        try:
            candidate_real = os.path.realpath(os.path.join(root_real, candidate))
            contained = (candidate_real == root_real
                         or candidate_real.startswith(root_real + os.sep))
            if not contained:
                return candidate
            if os.path.isfile(candidate_real):
                return f"{candidate} [present, {os.path.getsize(candidate_real)} bytes]"
            if "/" in candidate:
                return f"{candidate} [MISSING on disk]"
            # Bare filename-shaped token (no directory component) that doesn't
            # exist: could be a version pin, a URL tail fragment, or any other
            # non-path token that merely happens to look path-shaped. Asserting
            # "MISSING on disk" here would be noise, not a fact — leave it
            # untouched.
            return candidate
        except Exception:
            return candidate

    try:
        return _ARTIFACT_PATH_RE.sub(_stamp, text)
    except Exception:
        return text


_OUTCOME_INSTRUCTION = (
    "When you are finished, end your final message with ONE line:\n"
    "OUTCOME: <the concrete ids/paths/urls you produced, or NONE plus why>\n"
    "If the acceptance requires an external action you cannot or should not execute "
    "(a disabled capability, a missing credential, or something needing owner approval), "
    "do NOT report success — end instead with ONE line:\n"
    "OUTCOME: BLOCKED — <exactly what you need to proceed>"
)


def build_goal_run_task(goal: Goal, objective: Optional[Goal], *,
                        workspace_root: Optional[Path] = None) -> str:
    """``workspace_root`` is opt-in (default None = byte-identical to before T9):
    when provided, the goal's title/body/acceptance are stamped with
    ``stamp_artifact_references`` so a retried goal sees what's ACTUALLY on disk
    rather than trusting a stale ``last failure``/title/acceptance reference."""
    parts = []
    if objective is not None:
        parts.append(
            "STANDING OBJECTIVE (all work must advance it):\n"
            f"{objective.title}\n{objective.body}".rstrip())
    else:
        parts.append("This is a one-off goal (no standing objective attached).")
    goal_title = goal.title
    goal_body = goal.body or ""
    acceptance = (goal.payload or {}).get("acceptance")
    if workspace_root is not None:
        goal_title = stamp_artifact_references(goal_title, workspace_root)
        goal_body = stamp_artifact_references(goal_body, workspace_root)
        if acceptance:
            acceptance = stamp_artifact_references(str(acceptance), workspace_root)
    parts.append(f"GOAL: {goal_title}\n{goal_body}".rstrip())
    if acceptance:
        parts.append(f"Definition of done (acceptance): {acceptance}")
    # §5.2: retries are no longer amnesiac — the compact attempt ledger
    # (payload.attempts, appended by board.record_failure) rides into the
    # retry prompt so the run can address the SPECIFIC prior gap instead of
    # re-deriving everything from the title.
    attempts = (goal.payload or {}).get("attempts") or []
    if attempts:
        last = attempts[-1] if isinstance(attempts[-1], dict) else {}
        # 2026-07-14 night-2: when the OWNER fulfilled the blocking ask AFTER the
        # last failure, the failure ledger is stale — say so explicitly, or the
        # run declares BLOCKED from memory without ever retrying the real action.
        unblocked = (goal.payload or {}).get("owner_unblocked") or {}
        try:
            unblocked_after_failure = float(unblocked.get("ts") or 0) >= float(last.get("ts") or 0)
        except (TypeError, ValueError):
            unblocked_after_failure = False
        if unblocked and unblocked_after_failure:
            lines = [
                "OWNER UNBLOCKED THIS GOAL: the owner has FIXED the blocker since the "
                "previous attempt(s) — capabilities, allowlists or credentials have "
                "changed. Do NOT declare BLOCKED based on past failure memory; actually "
                "retry the real actions first and only report BLOCKED if a fresh attempt "
                "fails now.",
                f"- previously failed with: {str(last.get('error') or 'unknown')[:300]}",
            ]
        else:
            lines = [
                "PREVIOUS ATTEMPT (this goal was retried — address the gap, don't repeat it):",
                f"- last failure: {str(last.get('error') or 'unknown')[:500]}",
            ]
            if len(attempts) > 1:
                lines.append(f"- attempts so far: {len(attempts)}")
        parts.append("\n".join(lines))
    parts.append(_OUTCOME_INSTRUCTION)
    return "\n\n".join(parts)


