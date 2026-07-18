"""§4.1 evidence pack — collected mechanically at run end (no LLM, no goal-type
knowledge). Absorbs proposal 007 (episodes.artifacts stub, empty in all 230+
episodes ever recorded).

The pack rides ``RunOutcome.evidence`` into episodes and goal_events so every
downstream consumer — the dispatcher, the completion judge (§4.3), digests,
scorecards — reads the same facts instead of re-deriving strings.

Everything is fail-open and bounded: a scan error degrades to the empty list
and never fails a finished run.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_LEDGER_LINES = 80
MAX_LINE_CHARS = 200
MAX_REFS = 20
MAX_ARTIFACTS = 20
ERRORS_TAIL_STEPS = 5  # errors in the final N steps

# Actions whose successful result is itself evidence of produced output
# (proposal 007's allowlist) — descriptors recorded even when no file lands
# in the workspace.
OUTPUT_ACTION_ALLOWLIST = frozenset({
    "twitter_post",
    "twitter_reply",
    "filesystem_write_file",
    "fs_write",
    "apply_patch",
    "str_replace",
    "run_code",
    "knowledge_ingest",
    "x402_request",
    "message",
    "email_send",
})

_URL_RE = re.compile(r"https?://[^\s'\"<>)\]]+")
_PATH_RE = re.compile(r"(?:workspace|project)/[\w][\w./\-]*")


@dataclass
class EvidencePack:
    """Mechanically collected run facts (§4.1)."""

    ledger: List[str] = field(default_factory=list)        # action → ok/ERROR head
    errors_tail: List[str] = field(default_factory=list)   # errors in the final steps
    captured_refs: List[str] = field(default_factory=list) # ids/urls from SUCCESSFUL results
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    checks: List[Dict[str, Any]] = field(default_factory=list)  # acceptance-check results (§4.4)


# ---------------------------------------------------------------------------
# Ledger walking (shares the RunOutcome readers)
# ---------------------------------------------------------------------------

def _walk_ledger(orchestrator: Any):
    """Yield (label, action_name, action, result) across all agents' steps."""
    from agents.task.runtime.run_outcome import _action_name

    if orchestrator is None:
        return
    try:
        agents = list((getattr(orchestrator, "agents", None) or {}).values())
    except Exception:
        return
    for agent in agents:
        label = "sub:" if getattr(agent, "_is_sub_agent", False) else ""
        try:
            steps = list(getattr(getattr(agent, "history", None), "history", None) or [])
        except Exception:
            continue
        for step in steps:
            try:
                actions = list(getattr(getattr(step, "model_output", None), "action", None) or [])
                results = list(getattr(step, "result", None) or [])
            except Exception:
                continue
            for i, action in enumerate(actions):
                result = results[i] if i < len(results) else None
                yield (label, _action_name(action), action, result)


def _result_head(result: Any, limit: int = 120) -> str:
    content = str(getattr(result, "extracted_content", "") or "").strip()
    return content[:limit]


def build_evidence(orchestrator: Any, *, workspace_dir: Optional[str] = None,
                   started_ts: Optional[float] = None) -> EvidencePack:
    """Assemble the pack from the resident orchestrator. Never raises."""
    pack = EvidencePack()
    try:
        seen_refs: set = set()
        step_errors: List[tuple] = []  # (step_index, text) for the tail selection
        idx = 0
        for label, name, _action, result in _walk_ledger(orchestrator):
            idx += 1
            if result is None:
                continue
            error = getattr(result, "error", None)
            if error:
                status = f"ERROR: {str(error)[:MAX_LINE_CHARS]}"
                step_errors.append((idx, f"{label}{name}: {str(error)[:MAX_LINE_CHARS]}"))
            else:
                head = _result_head(result)
                status = f"ok: {head}" if head else "ok"
                # Refs come from SUCCESSFUL results only — facts, not claims.
                content = str(getattr(result, "extracted_content", "") or "")
                for m in _URL_RE.findall(content) + _PATH_RE.findall(content):
                    if m not in seen_refs and len(pack.captured_refs) < MAX_REFS:
                        seen_refs.add(m)
                        pack.captured_refs.append(m)
            if len(pack.ledger) < MAX_LEDGER_LINES:
                pack.ledger.append(f"{label}{name} -> {status}")
        # errors in the final steps: keep the tail
        if step_errors:
            pack.errors_tail = [t for _, t in step_errors[-ERRORS_TAIL_STEPS:]]
    except Exception:
        logger.debug("build_evidence: ledger walk failed", exc_info=True)
    pack.artifacts = collect_artifacts(
        orchestrator, workspace_dir=workspace_dir, started_ts=started_ts)
    return pack


# ---------------------------------------------------------------------------
# Artifacts (proposal 007)
# ---------------------------------------------------------------------------

def _resolve_workspace_dir(orchestrator: Any) -> Optional[str]:
    """Session workspace dir via pm(); None when unresolvable (fail-open)."""
    try:
        session_id = getattr(orchestrator, "session_id", None)
        if not session_id:
            return None
        from agents.task.path import pm
        # On a shared project-root workspace (local CLI) a full scan would list
        # the whole repo — only per-session workspaces are scanned unbounded;
        # the shared case still works when a started_ts window is supplied.
        return str(pm().get_workspace_dir(session_id, getattr(orchestrator, "user_id", None)))
    except Exception:
        return None


def _session_started_ts(orchestrator: Any) -> Optional[float]:
    try:
        vals = []
        for agent in list((getattr(orchestrator, "agents", None) or {}).values()):
            created = getattr(getattr(agent, "state", None), "session_created_at", None)
            ts = getattr(created, "timestamp", None)
            if callable(ts):
                vals.append(float(ts()))
        return min(vals) if vals else None
    except Exception:
        return None


def _scan_workspace(workspace_dir: str, started_ts: Optional[float]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        if not workspace_dir or not os.path.isdir(workspace_dir):
            return out
        for root, dirs, files in os.walk(workspace_dir):
            # bounded: skip dot-dirs (git/venv caches on shared workspaces)
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in files:
                if fname.startswith("."):
                    continue
                path = os.path.join(root, fname)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if started_ts is not None and st.st_mtime < started_ts:
                    continue
                out.append({
                    "path": os.path.relpath(path, workspace_dir),
                    "bytes": int(st.st_size),
                    "mtime": int(st.st_mtime),
                })
                if len(out) >= MAX_ARTIFACTS:
                    return out
    except Exception:
        logger.debug("artifact workspace scan failed", exc_info=True)
    return out


def collect_artifacts(orchestrator: Any, *, workspace_dir: Optional[str] = None,
                      started_ts: Optional[float] = None) -> List[Dict[str, Any]]:
    """Best-effort artifact list: workspace file diff + ledger descriptors for
    real-output actions. Bounded, fail-open, no LLM."""
    arts: List[Dict[str, Any]] = []
    # 1) ledger descriptors — a successful allowlisted action IS produced output
    try:
        for label, name, _action, result in _walk_ledger(orchestrator):
            if label or name not in OUTPUT_ACTION_ALLOWLIST:
                continue
            if result is None or getattr(result, "error", None):
                continue
            arts.append({"kind": name, "detail": _result_head(result)})
            if len(arts) >= MAX_ARTIFACTS:
                return arts
    except Exception:
        logger.debug("artifact ledger scan failed", exc_info=True)
    # 2) workspace file scan (time-windowed when a start timestamp is known)
    if workspace_dir is None:
        workspace_dir = _resolve_workspace_dir(orchestrator)
    if started_ts is None:
        started_ts = _session_started_ts(orchestrator)
    remaining = MAX_ARTIFACTS - len(arts)
    if remaining > 0 and workspace_dir:
        arts.extend(_scan_workspace(workspace_dir, started_ts)[:remaining])
    return arts
