"""Objective-driven goal planner (replaces scripts/seed_goal_seeder.py).

The PROMPT IS BUILT BY CODE from live board + deliverables state — the agent
never re-derives its mission from a hardcoded theme list. Pure functions here;
the dispatcher owns triggering (flags, cooldown, quota) and session dispatch.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PLANNER_TOOLS = ["goal", "task"]
PLANNER_MAX_STEPS = 8


def planner_session_tools() -> list:
    """Planner session toolset. Under autonomous mode the planner also gets read-only
    web_fetch so 'is this duplicate / still true?' checks are grounded, not guessed."""
    from agents.task.constants import full_autonomy_enabled
    tools = list(PLANNER_TOOLS)
    if full_autonomy_enabled() and "web_fetch" not in tools:
        tools.append("web_fetch")
    return tools


def list_deliverables(root: Path, max_files: int = 40) -> List[Dict[str, Any]]:
    """name (relative), mtime iso, first markdown heading — depth <=2, dotfiles skipped."""
    out: List[Dict[str, Any]] = []
    if not root or not Path(root).is_dir():
        return out
    root = Path(root)
    candidates = sorted(
        (p for pattern in ("*", "*/*") for p in root.glob(pattern)
         if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts)),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    for p in candidates[:max_files]:
        heading = ""
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                for i, line in enumerate(fh):
                    if i > 40:
                        break
                    if line.startswith("# "):
                        heading = line[2:].strip()
                        break
        except OSError:
            pass
        out.append({
            "name": str(p.relative_to(root)),
            "mtime_iso": time.strftime("%Y-%m-%d %H:%M", time.gmtime(p.stat().st_mtime)),
            "heading": heading,
        })
    return out


def build_planner_prompt(board, user_id: str, deliverables_root: Optional[Path],
                         *, history_n: int = 10) -> str:
    from agents.task.goals.board import OBJ_ACTIVE

    objectives = board.objectives(user_id=user_id, status=OBJ_ACTIVE)
    done = [g for g in board.list(user_id=user_id, status="done", limit=history_n * 3)
            if g.kind == "goal"]
    done = sorted(done, key=lambda g: g.completed_at or 0, reverse=True)[:history_n]
    blocked = [g for g in board.list(user_id=user_id, status="blocked", limit=20)
               if g.kind == "goal"]
    ready = [g for g in board.list(user_id=user_id, status="ready", limit=20)
             if g.kind == "goal"]

    sections = ["You are planning your own work queue. Create goals that genuinely "
                "advance a standing objective below. If you create nothing, there are "
                "TWO valid, DIFFERENT outcomes — pick the true one:\n"
                "  (a) REAL BLOCKER: progress needs something only the owner can provide "
                "(a credential, a decision, access) — state that specific blocker as your "
                "summary so it reaches the owner.\n"
                "  (b) QUEUE HEALTHY: the objective is already well-covered and there is "
                "no non-duplicate work worth adding right now — say exactly \"queue "
                "healthy, nothing to add\". This is NORMAL and is NOT a blocker.\n"
                "Do NOT invent busywork, and do NOT report a routine empty/covered queue "
                "as a blocker."]

    def _obj_line(o) -> str:
        crit = (o.payload or {}).get("success_criteria")
        base = f"- id={o.id} [{o.title}] {o.body}".rstrip()
        return base + (f"\n    success criteria: {crit}" if crit else "")

    sections.append("STANDING OBJECTIVES (active):\n" + "\n".join(
        _obj_line(o) for o in objectives))

    if done:
        sections.append("RECENTLY DONE (title -> outcome):\n" + "\n".join(
            f"- {g.title} -> {(g.payload or {}).get('outcome') or '[no outcome recorded]'}"
            for g in done))
    if blocked:
        # T9: recall-vs-filesystem honesty — a blocked goal's recorded failure
        # text is stale memory, not ground truth. Stamp any workspace-relative
        # path it references with what's ACTUALLY on disk (fail-open, never
        # raises), so the planner can tell "still missing" from "owner already
        # fixed it" instead of re-deriving that from the error prose alone.
        try:
            from agents.task.goals.context import stamp_artifact_references
        except Exception:
            stamp_artifact_references = lambda text, root=None: text  # noqa: E731

        def _blocked_line(g) -> str:
            err = (g.last_failure_error or '?')[:120]
            if deliverables_root is not None:
                err = stamp_artifact_references(err, deliverables_root)
            return f"- {g.title} (error: {err})"

        sections.append("BLOCKED (do NOT recreate; fix or avoid):\n" + "\n".join(
            _blocked_line(g) for g in blocked))
    if ready:
        sections.append("ALREADY QUEUED (ready):\n" + "\n".join(f"- {g.title}" for g in ready))

    # Board-stall guard: 0 ready + blocked goals means the instance goes IDLE. "queue
    # healthy" is NOT valid here — it was the observed 14h-quiet stall (blocked -> ask ->
    # "queue healthy" -> idle). Force NEW achievable work or a single concrete owner-blocker.
    if not ready and blocked:
        sections.append(
            f"⚠️ STALLED BOARD: 0 ready goals, {len(blocked)} blocked. If you add nothing the "
            "instance goes idle — that is a FAILURE, not 'queue healthy'. You MUST create 1-3 "
            "NEW, DIFFERENT, achievable goals that AVOID the blocked goals' failure modes "
            "(smaller scope, a different approach, no dependency on an unmet owner-blocker), OR "
            "— only if EVERY path genuinely needs the owner — state the ONE specific blocker. "
            "Do NOT recreate a blocked goal and do NOT say 'queue healthy, nothing to add'.")

    dels = list_deliverables(deliverables_root) if deliverables_root else []
    if dels:
        sections.append("EXISTING DELIVERABLES (extend these; do NOT create overlapping docs):\n"
                        + "\n".join(f"- {d['name']} ({d['mtime_iso']})"
                                    + (f": {d['heading']}" if d['heading'] else "")
                                    for d in dels))

    # T8 (013 owner transparency directive): the planner's own session toolset is
    # NOT the ceiling for goals it creates — goals carry their OWN tools. Fail-open:
    # on any error, omit only the dynamic grantable-list line, never the whole prompt.
    ground_truth_lines = [
        "TOOL GROUND TRUTH:",
        "- Your OWN session toolset is NEVER the ceiling — goals carry their OWN tools.",
    ]
    try:
        from agents.task.agent.core.tool_availability import grantable_autonomous_tools
        ground_truth_lines.append(
            "- Goals you create may be granted: "
            + ", ".join(grantable_autonomous_tools()) + ".")
    except Exception:
        pass
    ground_truth_lines.append(
        "- Declaring 'REAL BLOCKER: <tool> unavailable' about YOUR OWN session is a "
        "category error and is FORBIDDEN. A REAL BLOCKER is exclusively something only "
        "the owner can provide (a credential, a decision, access) that is NOT in the "
        "grantable list above.")
    sections.append("\n".join(ground_truth_lines))

    sections.append(
        "INSTRUCTIONS:\n"
        "- Create 1-3 goals with goal_create; each MUST set objective_id, tools, and "
        "acceptance (what 'done' must prove: ids/paths/urls).\n"
        "- When the outcome is mechanically checkable, ALSO set acceptance_checks "
        "(typed, framework-executed) — a passed check is proof, prose is not. The ONLY "
        "valid check types are 'artifact_glob' ({'type':'artifact_glob','pattern':'*.md'}), "
        "'http_ok' ({'type':'http_ok','url':'…'}) and 'file_contains' "
        "({'type':'file_contains','path':'report.md','contains':['A','B'],'mode':'all'}); "
        "do NOT invent other types — an unknown type fail-closes and can never pass.\n"
        "- Each goal must EXTEND an existing deliverable or state why none applies.\n"
        "- Tools by shape: research -> ['web_fetch','anysite','filesystem','task']; "
        "drafting -> ['filesystem','task','web_fetch']; posting/engagement -> "
        "['twitter','filesystem','task']. At most ONE goal may include 'twitter'.\n"
        "- Never exceed 5 ready goals total. A rejected duplicate means: extend the "
        "matched goal's work instead of retrying a rename.\n"
        "- If progress is blocked on something only the owner can provide (credentials, "
        "a decision, access), SAY SO explicitly and specifically — a concrete ask beats "
        "inventing busywork.\n"
        "- If ≥3 blocked/failed goals reference the SAME missing artifact, create ONE ask "
        "that names the artifact and STOP queuing goals that depend on it until the ask is "
        "fulfilled.\n"
        "- Finish with a one-line summary of what you queued, or — if you queued "
        "nothing — the specific blocker/ask standing between you and the objective."
    )
    return "\n\n".join(sections)
