"""``/adopt`` — the owner makes an AGENT-authored cron job or goal his own.

Why (natural-work sweep 2026-10-08): an owner turn that read third-party
content writes AGENT-authored standing work (``tools.goal_tools.
read_taint_authorship_note``), and an autonomous run's own jobs are the
agent's too. Such a row runs under agent limits — the self-goal tool ceiling,
no X post or room moderation without a per-run approval. The only way to make
it the owner's was to cancel it and have it created again in a clean owner turn.

``/adopt <id>`` shows what the row will do — its task, schedule, rig, tools,
target, read/write verb and delivery — and ends with a confirm line, which every
seat turns into an action card (``core.surfaces.cards``, ``/adopt`` is in
``CONFIRMABLE_VERBS``). The owner's Confirm is the authority: it re-runs
``/adopt <id> <fingerprint> go`` through the seat's own owner gate, and only
then is ``payload.authored_by`` set to ``owner``.

⚠️ What adoption never does:

* run for the agent — this is an owner-seat verb (room-refused, owner-gated);
  the agent's ``propose_action`` can only PROPOSE it, which runs the quote;
* adopt a row that changed after the owner saw it — the confirm line carries a
  fingerprint of the task, schedule and payload, and a mismatch refuses;
* grant money — money tool ids in ``payload.tools`` (or a rig that resolves to
  one) are dropped on adoption; the owner grants money himself;
* trust the row's buy target — it is stamped restrict-only (``AGENT``), as for
  any target the owner did not type;
* hide anything — the task/body is shown in full (a long one arrives over
  several messages), every payload key is listed, and each pinned skill is
  shown with a content digest that is BOUND on adoption: a skill edited later
  makes the row run as the agent's again until the owner re-adopts it
  (``agents.task.agent.skill_pins``).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, List, Optional, Tuple

USAGE = ("Usage: /adopt — list the agent-authored jobs and goals\n"
         "/adopt <id> — show one, then confirm to make it yours")

_GO = ("go", "execute", "confirm")


def _money_ids() -> frozenset:
    from core.tool_capabilities import ids_with
    return ids_with("money")


def _agent_authored(payload, user_id: Optional[str] = None) -> bool:
    """Not OWNER-authored — the agent's, an unstamped legacy row, or an adopted
    row whose pinned skills changed after the owner saw them."""
    from core.config_policy.rigs import is_owner_authored
    if not is_owner_authored(payload):
        return True
    from agents.task.agent.skill_pins import adoption_lapsed
    return adoption_lapsed(payload, user_id)


def _cron_service(data_dir: str):
    from core.runtime_paths import cron_db_path
    from cron.jobs import CronJobStore
    from cron.service import CronService
    return CronService(CronJobStore(cron_db_path(data_dir)))


def _board(data_dir: str, board: Any = None):
    if board is not None:
        return board
    from agents.task.goals.board import GoalBoard
    from core.runtime_paths import goals_db_path
    return GoalBoard(goals_db_path(data_dir))


def _candidates(user_id: str, data_dir: str, board: Any = None) -> List[Tuple[str, Any]]:
    """Every LIVE agent-authored row of this tenant: ``("cron"|"goal", row)``."""
    from core.goal_vocab import KIND_ASK, LIVE_STATUSES
    out: List[Tuple[str, Any]] = []
    for job in _cron_service(data_dir).list_jobs(user_id=user_id):
        if job.status in ("scheduled", "running") and _agent_authored(job.payload, user_id):
            out.append(("cron", job))
    for goal in _board(data_dir, board).list(user_id=user_id):
        if (goal.kind != KIND_ASK and goal.status in LIVE_STATUSES
                and _agent_authored(goal.payload, user_id)):
            out.append(("goal", goal))
    return out


def _skills(row: Any) -> List[Tuple[str, str]]:
    """``[(skill id, digest)]`` of the row's pinned skills (``payload.skills``)."""
    from core.config_policy.rigs import pinned_skills
    from agents.task.agent.skill_pins import skill_digests
    ids = pinned_skills(row.payload or {})
    return skill_digests(ids, getattr(row, "user_id", None)) if ids else []


def fingerprint(kind: str, row: Any) -> str:
    """A positive decimal bound to what the owner was shown (task, schedule,
    payload, and the CONTENT of every pinned skill). A card's confirm line may
    carry one number; a row — or a pinned skill — edited after the quote no
    longer matches it."""
    payload = dict(row.payload or {})
    if kind == "cron":
        doc = {"task": row.task, "schedule": row.schedule_spec, "payload": payload}
    else:
        doc = {"title": row.title, "body": row.body, "payload": payload}
    doc["skills"] = _skills(row)
    digest = hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()
    return str(int(digest[:12], 16) + 1)


def _money_in(payload: dict) -> Tuple[List[str], bool]:
    """``(money tool ids in payload.tools, rig resolves to a money tool)``."""
    from core.config_policy.rigs import is_rig, rig_tools
    money = _money_ids()
    tools = [t for t in (payload.get("tools") or []) if t in money]
    rig = payload.get("rig")
    rig_money = bool(rig and is_rig(rig) and set(rig_tools(rig) or []) & money)
    return tools, rig_money


#: Payload keys the description renders itself (or that are bookkeeping).
_SHOWN_KEYS = frozenset({
    "rig", "tools", "target_token", "write_verb", "read_verb", "deliver", "skills",
    "authored_by", "legacy_unstamped", "adopted_skills_digest", "adoption_lapsed",
    "created_by_session_id",
})


def _describe(kind: str, row: Any) -> List[str]:
    """Everything the row will do — never cut. A long task arrives split over
    several messages (every seat splits a long reply); nothing is dropped."""
    p = dict(row.payload or {})
    lines = []
    if p.get("legacy_unstamped"):
        lines.append("(Written before authorship was recorded — it may be Rob's or yours.)")
    if p.get("adopted_skills_digest") and p.get("authored_by") == "owner":
        lines.append("(Adopted before, but a pinned skill changed since — it runs as "
                     "Rob's until you adopt it again.)")
    if kind == "cron":
        lines.append(f"Cron job {row.id} [{row.status}] · schedule {row.schedule_spec}")
        lines.append(f"Task ({len(row.task or '')} chars, in full):")
        lines.append(row.task or "")
    else:
        lines.append(f"Goal {row.id} [{row.status}]: {row.title or ''}")
        if row.body:
            lines.append(f"Body ({len(row.body)} chars, in full):")
            lines.append(row.body)
    lines.append(f"Rig: {p.get('rig') or 'default'}")
    lines.append("Tools: " + (", ".join(p.get("tools") or []) or "the rig's"))
    target = p.get("target_token")
    if target:
        lines.append(f"Target: {target.get('address')} on {target.get('chain')} "
                     "(stays restrict-only: a buy of it is not trusted)")
    for key, label in (("write_verb", "Write verb"), ("read_verb", "Read verb")):
        if p.get(key):
            lines.append(f"{label}: {json.dumps(p[key], sort_keys=True)}")
    if p.get("deliver"):
        lines.append(f"Delivers to: {p['deliver']}")
    skills = _skills(row)
    if skills:
        lines.append("Pinned skills (their text is part of what you adopt; an edit "
                     "after this un-adopts the row):")
        for sid, dg in skills:
            lines.append(f"  • {sid} — " + ("NOT FOUND" if dg == "missing"
                                             else f"content sha256 {dg[:16]}"))
    other = {k: v for k, v in p.items() if k not in _SHOWN_KEYS}
    if other:
        lines.append("Other settings (in full):")
        for k in sorted(other):
            lines.append(f"  {k}: {json.dumps(other[k], sort_keys=True, default=str)}")
    money_tools, rig_money = _money_in(p)
    if money_tools or rig_money:
        dropped = ", ".join(money_tools + ([f"rig {p.get('rig')}"] if rig_money else []))
        lines.append(f"Not adopted: {dropped} — money is never granted by adoption; "
                     "grant it yourself.")
    return lines


def adopt_reply(user_id: str, data_dir: str, args: List[str], *, board: Any = None) -> str:
    """The ``/adopt`` answer on every seat (Telegram, console, REPL)."""
    from surfaces.telegram.owner_ops import resolve_prefix
    words = [str(a) for a in (args or []) if str(a).strip()]
    rows = _candidates(user_id, data_dir, board)
    if not words:
        if not rows:
            return "No agent-authored cron jobs or goals — nothing to adopt."
        out = [f"{len(rows)} agent-authored row(s):"]
        for kind, row in rows[:20]:
            what = row.task if kind == "cron" else row.title
            p = row.payload or {}
            mark = (" (legacy, unstamped)" if p.get("legacy_unstamped")
                    else " (skill changed since adoption)" if p.get("authored_by") == "owner"
                    else "")
            out.append(f"• {kind} {row.id[:8]}{mark} — {(what or '')[:80]}…"
                       if len(what or "") > 80 else
                       f"• {kind} {row.id[:8]}{mark} — {what or ''}")
        out.append("Adopt one: /adopt <id>")
        return "\n".join(out)
    go = words[-1].lower() in _GO
    rest = words[:-1] if go else words
    if len(rest) not in (1, 2) or (len(rest) == 2 and not rest[1].isdecimal()):
        return USAGE
    by_id = {row.id: (kind, row) for kind, row in rows}
    row_id, err = resolve_prefix(rest[0], list(by_id))
    if err:
        return f"{err} among the agent-authored rows — see /adopt."
    kind, row = by_id[row_id]
    fp = fingerprint(kind, row)
    if not go:
        return "\n".join(
            _describe(kind, row)
            + ["Adopting makes it OWNER-authored: its rig and tools run as written (no agent "
               "ceiling), and it may post and moderate without a per-run approval.",
               f"To adopt it: /adopt {rest[0]} {fp} go"])
    if len(rest) == 2 and rest[1] != fp:
        return (f"❌ Not adopted: {kind} {row_id[:8]} changed after you saw it. "
                f"Run /adopt {rest[0]} again and check it.")
    return _adopt(kind, row, user_id, data_dir, board)


def _adopt(kind: str, row: Any, user_id: str, data_dir: str, board: Any) -> str:
    from core.config_policy.rigs import AUTHORED_BY_KEY, OWNER_AUTHOR
    from core.wallet.buy_target import AGENT, PAYLOAD_KEY, TARGET_AUTHOR_KEY
    from agents.task.agent.skill_pins import ADOPTED_SKILLS_KEY, LAPSED_KEY, combined
    p = dict(row.payload or {})
    skills = _skills(row)
    # The pinned skills' content is bound: an edit after this un-adopts the row
    # (``agents.task.agent.skill_pins.adoption_lapsed``, read on every run).
    updates: dict = {AUTHORED_BY_KEY: OWNER_AUTHOR,
                     ADOPTED_SKILLS_KEY: combined(skills) if skills else None,
                     "legacy_unstamped": None, LAPSED_KEY: None}
    money_tools, rig_money = _money_in(p)
    if money_tools:
        kept = [t for t in (p.get("tools") or []) if t not in money_tools]
        updates["tools"] = kept or None
    if rig_money:
        updates["rig"] = None
    if p.get(PAYLOAD_KEY):
        updates[TARGET_AUTHOR_KEY] = AGENT
    if kind == "cron":
        from cron.rig_edit import set_job_payload_key
        store = _cron_service(data_dir).store
        ok = all(set_job_payload_key(store, row.id, k, v, user_id=user_id)
                 for k, v in updates.items())
    else:
        from core.sqlite_util import execute_retry
        merged = {k: v for k, v in {**p, **updates}.items() if v is not None}
        ok = bool(execute_retry(_board(data_dir, board).db_path,
                                "UPDATE goals SET payload=? WHERE id=? AND user_id=?",
                                (json.dumps(merged), row.id, user_id)))
    if not ok:
        return f"❌ Could not adopt {kind} {row.id[:8]} — see /adopt."
    note = (" Money tools were not adopted." if (money_tools or rig_money) else "")
    return f"✅ Adopted {kind} {row.id[:8]}: it is owner-authored now.{note}"


async def adopt_verb(*, user_id: str, data_dir: str, args: List[str],
                     task_agent: Any = None, result: Any = None,
                     board: Any = None) -> str:
    """Telegram (and console) handler — run after the seat's owner gate."""
    return adopt_reply(user_id, data_dir, list(args or []), board=board)


__all__ = ["USAGE", "adopt_reply", "adopt_verb", "fingerprint"]
