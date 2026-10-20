"""Status sections: ``work`` (the goal board) and ``approvals``.

Moved out of ``core/status_snapshot.py`` (W9, 2026-10-10 structural review)
unchanged. ``core.status_snapshot`` stays the ONE assembler and re-exports
every name here; the store reads go through ``core.status_snapshot._rows``
late-bound, so a test that patches it still reaches this builder.
"""
from __future__ import annotations

from core.runtime_paths import correspondents_db_path
import os
from typing import Any, Dict, List

from core import goal_vocab as _gv
from core.status_types import (  # noqa: F401
    ASK_KIND_TOKEN_IDENTITY, HealthItem, SEVERITY_CRIT, SEVERITY_WARN, Section,
    _ASK_OPEN, _KIND_ASK, _KIND_GOAL, _K_GOAL_RUN,
    _ST_BLOCKED, _ST_READY, _ST_RUNNING, _ST_WAITING, _TOOL_APPROVAL_ASK_KIND, _age,
    _flag_command, _payload, _tele_rows, has_own_surface, snapshot_rows as _rows,
)


#: The board's own marker for a provider-exhausted failure
#: (``agents.task.goals.dispatcher.LLM_EXHAUSTED_MARKER``; core may not import it).
_LLM_EXHAUSTED = "llm_provider_exhausted"


def _block_reason(b: Dict[str, Any]) -> tuple:
    """``(kind, why, remedy)`` for one blocked goal, in owner words (080 WS-4).
    The error the board stored is the WHY; the kind says who can fix it."""
    err = str(b.get("error") or "").strip()
    gid = b["id"]
    low = err.lower()
    if low.startswith("agent declared blocked"):
        need = err.split(":", 1)[1].strip() if ":" in err else ""
        return ("it needs something from you", need or "it did not say what",
                f"give it what it needs, then /goal retry {gid} — or /goal stop {gid}")
    if low.startswith(_LLM_EXHAUSTED):
        return ("no model provider could run it", err[:160],
                f"fix the provider key or credit, then /goal retry {gid}")
    if low.startswith("reclaimed"):
        return ("its run crashed", err[:160], f"/goal retry {gid} — or /goal stop {gid}")
    fails = f"failed {b['failures']}/{b['max_retries']} times"
    return (fails, err[:160] or "no error was recorded",
            f"/goal retry {gid} after the cause is fixed, or /goal stop {gid}")


def _open_goals_and_stops(sec: Section, user_id: str, goals_db: str, now: float) -> None:
    """``sec.data["open_goals"]`` (every live goal, board order) and
    ``sec.data["stops"]`` (what the owner switched OFF, every scope) for the
    agent's ``<operations>`` block. A stop store that cannot be read is named in
    ``stops_reason`` — never read as "nothing is off"."""
    live = tuple(getattr(_gv, "LIVE_STATUS_ORDER", ()))
    marks = ",".join("?" for _ in live)
    rows = _rows(goals_db, f"SELECT id, title, status FROM goals WHERE kind=? AND user_id=? "
                           f"AND status IN ({marks}) ORDER BY priority DESC, created_at",
                 (_KIND_GOAL, user_id) + live)
    sec.data["open_goals"] = [{"id": r["id"], "title": r["title"] or "",
                               "status": r["status"]} for r in rows]
    try:
        from core import goal_suppressions as _gs
        sec.data["stops"] = [{"scope": s.scope, "label": s.label, "since": s.created_at}
                             for s in _gs.active(goals_db, user_id=user_id, now=now)]
    except Exception as e:
        sec.data["stops"] = None
        sec.data["stops_reason"] = f"{type(e).__name__}: {str(e)[:120]}"


def _work_section(user_id: str, goals_db: str, tele: Section, now: float) -> Section:
    sec = Section(name="work")
    counts = {r["status"]: int(r["n"]) for r in _rows(
        goals_db, "SELECT status, COUNT(*) AS n FROM goals WHERE kind=? AND user_id=? "
                  "GROUP BY status", (_KIND_GOAL, user_id))}
    ready, running = counts.get(_ST_READY, 0), counts.get(_ST_RUNNING, 0)
    blocked_n, waiting = counts.get(_ST_BLOCKED, 0), counts.get(_ST_WAITING, 0)
    sec.data["counts"] = counts
    # Open asks (tool-approval asks belong to the approvals section).
    asks = []
    for r in _rows(goals_db, "SELECT id, title, body, payload, created_at FROM goals "
                             "WHERE kind=? AND status=? AND user_id=? ORDER BY created_at",
                   (_KIND_ASK, _ASK_OPEN, user_id)):
        p = _payload(r)
        if has_own_surface(p):
            continue
        asks.append({"id": r["id"], "title": r["title"], "created_at": r["created_at"],
                     "blocks": list(p.get("blocks_goal_ids") or [])})
    sec.data["open_asks"] = asks
    blocked = [{"id": r["id"], "title": r["title"],
                "failures": int(r.get("consecutive_failures") or 0),
                "max_retries": int(r.get("max_retries") or 0),
                "error": (r.get("last_failure_error") or "")[:160]}
               for r in _rows(goals_db, "SELECT id, title, consecutive_failures, max_retries, "
                                        "last_failure_error FROM goals WHERE kind=? AND status=? "
                                        "AND user_id=? ORDER BY created_at",
                              (_KIND_GOAL, _ST_BLOCKED, user_id))]
    sec.data["blocked"] = blocked
    _open_goals_and_stops(sec, user_id, goals_db, now)
    line =f"{ready} ready, {running} running, {blocked_n} blocked, {waiting} waiting"
    # 080 WS-4: an owner-paused goal is the owner's choice, not a health problem:
    # it is counted here and never raised as "fix the cause".
    paused_n = counts.get(getattr(_gv, "STATUS_PAUSED", "paused"), 0)
    if paused_n:
        line += f" · {paused_n} paused by you (/goal retry <id> to resume)"
    sec.lines.append(line)
    if tele.available:
        runs = _tele_rows(tele, _K_GOAL_RUN)
        by = {}
        for r in runs:
            o = (r["attrs"] or {}).get("outcome") or "?"
            by[o] = by.get(o, 0) + 1
        sec.data["runs_window"] = by
        sec.lines.append("24h goal runs: " + (", ".join(f"{k}={v}" for k, v in sorted(by.items()))
                                             or "none"))
        # 057 WS-C: a pre-emption is a first-class outcome — the 16 yields of
        # 2026-09-19 were counted by hand because nothing rendered them.
        yielded = [r for r in runs if (r["attrs"] or {}).get("outcome") == "yielded"]
        resumed = [r for r in runs if (r["attrs"] or {}).get("outcome") == "resumed"]
        deferred = [r for r in runs if (r["attrs"] or {}).get("outcome") == "deferred"]
        if yielded or resumed or deferred:
            per_goal: Dict[str, int] = {}
            for r in yielded:
                g = str((r["attrs"] or {}).get("goal_id") or "?")[:12]
                per_goal[g] = per_goal.get(g, 0) + 1
            reasons: Dict[str, int] = {}
            for r in deferred:
                rs = str((r["attrs"] or {}).get("reason") or "?").split(":")[0]
                reasons[rs] = reasons.get(rs, 0) + 1
            worst = max(per_goal.items(), key=lambda kv: kv[1]) if per_goal else None
            line = f"pre-emption 24h: yielded ×{len(yielded)} · resumed ×{len(resumed)}"
            if deferred:
                line += " · deferred ×%d (%s)" % (
                    len(deferred), ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())))
            if worst and worst[1] >= 3:
                line += f" · goal {worst[0]} yielded {worst[1]}×"
            sec.lines.append(line)
            sec.data["preemption"] = {"yielded": len(yielded), "resumed": len(resumed),
                                      "deferred": len(deferred), "per_goal": per_goal}
            if yielded and not resumed and len(yielded) >= 3:
                sec.health.append(HealthItem(
                    key="yields_not_resumed", severity=SEVERITY_WARN,
                    text=f"{len(yielded)} goal yields in 24h and no resume — each restart "
                         f"re-reads the world from scratch",
                    remedy=(_flag_command("GOAL_RESUME_SAME_SESSION")
                            + " and " + _flag_command("GOAL_YIELD_GRACE_SEC", "90")
                            + "; mark read-only jobs `polyrob job edit <id> --no-preempts`")))
    else:
        sec.lines.append(f"24h goal-run counts unavailable ({tele.reason})")
    for a in asks:
        age = _age(a["created_at"], now)
        blocks = f", blocks {len(a['blocks'])} goal(s)" if a["blocks"] else ""
        sec.health.append(HealthItem(
            key=f"ask:{a['id']}", severity=SEVERITY_CRIT if a["blocks"] else SEVERITY_WARN,
            text=f"open ask ({age} old{blocks}): {a['title'][:110]}",
            remedy=f"/asks · /fulfill {a['id']} once done"))
    for b in blocked:
        kind, why, remedy = _block_reason(b)
        sec.health.append(HealthItem(
            key=f"blocked:{b['id']}", severity=SEVERITY_WARN,
            text=f"goal BLOCKED — {kind}: {b['title'][:100]}" + (f" — {why}" if why else ""),
            remedy=remedy))
    return sec


def _approvals_section(user_id: str, data_dir: str, goals_db: str) -> Section:
    sec = Section(name="approvals")
    items: List[Dict[str, Any]] = []
    open_asks = _rows(goals_db, "SELECT id, title, payload FROM goals WHERE kind=? "
                                "AND status=? AND user_id=?",
                      (_KIND_ASK, _ASK_OPEN, user_id))
    for r in open_asks:
        kind = _payload(r).get("ask_kind")
        if kind == _TOOL_APPROVAL_ASK_KIND:
            items.append({"kind": _TOOL_APPROVAL_ASK_KIND, "id": f"tap-{r['id']}",
                          "preview": (_payload(r).get("tool_name") or r["title"] or "")[:80]})
        elif kind == ASK_KIND_TOKEN_IDENTITY:
            # W1: which contract is the real token — decided on /pending.
            items.append({"kind": ASK_KIND_TOKEN_IDENTITY, "id": r["id"],
                          "preview": (r["title"] or "")[:80]})
    from core import self_evolution
    from core.instance import resolve_instance_id
    for it in self_evolution.list_pending(user_id, home_dir=data_dir,
                                          instance_id=resolve_instance_id()) or []:
        items.append({"kind": it.get("kind"), "id": it.get("id"),
                      "preview": (it.get("preview") or "")[:80]})
    corr_db = correspondents_db_path(data_dir)
    if os.path.exists(corr_db):
        from core.surfaces.correspondents import CorrespondentRegistry
        from core.surfaces.owner_admin import pending_correspondent_items
        try:
            for it in pending_correspondent_items(CorrespondentRegistry(corr_db),
                                                  user_id):
                items.append({"kind": it.get("kind"), "id": it.get("id"),
                              "preview": (it.get("preview") or "")[:80]})
        except Exception as e:
            # AC1: an unreadable correspondent store is named, never read as
            # "no pending contacts"; the other approvals still show.
            sec.health.append(HealthItem(
                key="pending_contacts_unreadable", severity=SEVERITY_WARN,
                text=(f"pending contacts unreadable ({type(e).__name__}) — "
                      f"a contact may be waiting on you"),
                remedy="/pending"))
    sec.data["items"] = items
    # O18: the owner's open asks (owner_ask / goal asks) are decisions too; they
    # are answered on /asks, not /pending, so they are counted beside the
    # approvals rather than folded into ``items``. Same rows, no second read.
    asks_open = sum(1 for r in open_asks if not has_own_surface(_payload(r)))
    sec.data["open_asks"] = asks_open
    if items:
        line = f"{len(items)} pending approval(s)"
    elif any(h.key == "pending_contacts_unreadable" for h in sec.health):
        line = "no other pending approvals; pending contacts unreadable"
    else:
        line = "no pending approvals"
    if asks_open:
        line += f"; {asks_open} open ask(s) — /asks"
    sec.lines.append(line)
    if items:
        kinds = sorted({str(i.get("kind")) for i in items})
        sec.health.append(HealthItem(
            key="pending_approvals", severity=SEVERITY_CRIT,
            text=f"{len(items)} decision(s) waiting on you ({', '.join(kinds)})",
            # 2026-09-15: `/approve <id>` made the reader copy an id off a
            # phone — a chat client links the verb and not its argument. The
            # tappable token per item lives in `/pending`; the whole queue is
            # one token of its own.
            remedy="/pending · /approve_all · /reject_all"))
    return sec
