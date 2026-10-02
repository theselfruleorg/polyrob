"""``/rail`` — standing work from the owner's seat (036 §4.1). ONE implementation.

Telegram calls :func:`rail_reply`; the REPL wraps it (``cli/ui/commands/h_rail.py``)
and ``polyrob rails`` renders through the same functions, so a rail reads and
behaves identically on every seat.

⚠️ REACH, never policy. The rules live below this file:
``agents/task/goals/rails.py`` (the definition, the seeder) and
``core/tool_grants.py`` (the grant store, the ONE predicate). Everything here
applies immediately from a genuine owner turn EXCEPT ``grant``, which needs a
typed confirmation naming the tool and the rail (035: a rule that grants may not
apply at once; one that removes may). ``revoke`` is free.
"""
from __future__ import annotations

import logging
import shlex
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

USAGE = (
    "Usage:\n"
    "/rail — list rails (ON / OFF / UNDECLARED)\n"
    "/rail show <name>\n"
    "/rail templates\n"
    "/rail new <template> [slot=value …]   e.g. /rail new topic-watch topic=\"AI agents\" interval=24h\n"
    "/rail edit <name> body \"…\" | schedule \"every 24h\" | max <n> | priority <n> | title \"…\"\n"
    "/rail on|off <name>\n"
    "/rail drop <name> [--cancel-live]\n"
    "/rail adopt <objective id> <schedule>\n"
    "/rail grant <name> <tool>   (asks you to confirm)\n"
    "/rail revoke <name> <tool>\n"
    "/rail grants\n"
    "/rail proposals | accept <key> | dismiss <key>   (a dismissed key is never re-offered)")


def _board(data_dir: str, board: Optional[Any] = None):
    if board is not None:
        return board
    from agents.task.goals.board import GoalBoard
    from core.runtime_paths import goals_db_path
    return GoalBoard(goals_db_path(data_dir))


def _split(args: List[str]) -> List[str]:
    raw = " ".join(str(a) for a in (args or []))
    try:
        return shlex.split(raw)
    except ValueError:
        return raw.split()


def _when(ts: Optional[float]) -> str:
    if not ts:
        return "never"
    return time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(float(ts)))


# --- the ONE renderer (every seat) ----------------------------------------------------


def render_list(board: Any, user_id: str) -> str:
    from agents.task.goals import rails as R
    try:
        rows = R.list_rails(board, user_id)
        loose = R.undeclared(board, user_id)
    except Exception as e:
        return f"Rails: unavailable({type(e).__name__}: {e})"
    on = [r for r in rows if r.status == "active"]
    off = [r for r in rows if r.status != "active"]
    lines: List[str] = []
    if not rows and not loose and not _proposals(board, user_id):
        return ("No rails. Standing work is yours to set up — /rail templates, then "
                "/rail new <template>.")
    now = time.time()

    def _line(r) -> str:
        rec = R.recurrence_of(r) or {}
        try:
            nxt = R.next_seed_at(board, r, now)
            when = "due now" if nxt is not None and nxt <= now else (
                "next " + _when(nxt) if nxt else "spent")
        except Exception:
            when = "schedule unreadable"
        live = R.rail_live(board, user_id, r.id)
        grants = _grant_names(board, user_id, r.id)
        gtxt = f" · grants {', '.join(grants)}" if grants else ""
        return (f"• {R.rail_name(r)} — {rec.get('schedule')} · {live} live · {when}{gtxt}")

    if on:
        lines.append("ON")
        lines.extend(_line(r) for r in on)
    if off:
        lines.append("OFF")
        lines.extend(_line(r) for r in off)
    proposed = _proposals(board, user_id)
    if proposed:
        lines.append("PROPOSED (nothing runs until you accept)")
        lines.extend(_proposal_line(p) for p in proposed)
    if loose:
        lines.append("UNDECLARED (adopt as a rail, or drop)")
        for o in loose:
            lines.append(f"• {o.id[:8]} {o.title[:80]} — /rail adopt {o.id[:8]} every 24h  "
                         f"or  /goal objective drop {o.id[:8]}")
    return "\n".join(lines)


def _grant_names(board: Any, user_id: str, rail_id: str, *, pending: bool = False) -> List[str]:
    from core.tool_grants import grants_for
    rows = grants_for(getattr(board, "db_path", None), user_id=user_id, rail_id=rail_id,
                      include_pending=True)
    return [g.tool_id + (" (pending)" if g.pending else "") for g in rows
            if pending or not g.pending] if rows else []


def render_show(board: Any, user_id: str, row: Any) -> str:
    from agents.task.goals import rails as R
    from core.tool_grants import grants_for, is_money, money_armed
    rec = R.recurrence_of(row) or {}
    now = time.time()
    try:
        due, why = R.rail_is_due(board, user_id, row, now)
    except Exception as e:
        due, why = False, f"unreadable: {e}"
    lines = [f"{R.rail_name(row)} — {'ON' if row.status == 'active' else 'OFF (' + row.status + ')'}",
             f"id {row.id} · schedule {rec.get('schedule')} · max live "
             f"{rec.get('max_live', len(rec.get('legs') or []))}",
             f"next seed: {'due now' if due else why} · last seeded "
             f"{_when(R.last_seeded_at(board, row))}"]
    if row.body:
        lines.append(row.body[:400])
    if rec.get("rig"):
        lines.append(f"rig: {rec['rig']}")
    lines.append("legs:")
    for i, leg in enumerate(rec.get("legs") or [], 1):
        flag = " (independent)" if leg.get("independent") else ""
        lines.append(f"  {i}. {leg['title']}{flag}")
    grants = grants_for(board.db_path, user_id=user_id, rail_id=row.id, include_pending=True)
    if grants:
        armed = money_armed()
        lines.append("grants:")
        for g in grants:
            state = "PENDING — /rail grant to confirm" if g.pending else "active"
            if not g.pending and is_money(g.tool_id) and not armed:
                state = "inert — the money regime is not armed"
            lines.append(f"  {g.tool_id} — {state}; by {g.granted_by} at {_when(g.granted_at)}")
    else:
        lines.append("grants: none (the ordinary goal toolset)")
    recent = sorted(R.rail_goals(board, user_id, row.id),
                    key=lambda g: g.created_at or 0, reverse=True)[:5]
    if recent:
        lines.append("last seeds:")
        for g in recent:
            lines.append(f"  {g.id[:8]} {g.status} — {g.title[:70]}")
    return "\n".join(lines)


# --- the verb ----------------------------------------------------------------------------


def rail_reply(user_id: Optional[str], data_dir: str, args: List[str],
               board: Optional[Any] = None, seat: str = "telegram") -> str:
    """``/rail …`` from an owner seat. Returns the reply text."""
    if not user_id:
        return "Only the owner can manage rails."
    words = _split(args)
    verb = (words[0].lower() if words else "list")
    rest = words[1:]
    b = _board(data_dir, board)
    try:
        if verb in ("list", "ls"):
            return render_list(b, user_id)
        if verb == "templates":
            return _templates_reply()
        if verb == "grants":
            return _grants_reply(b, user_id)
        if verb == "new":
            return _new_reply(b, user_id, rest, seat)
        if verb == "adopt":
            return _adopt_reply(b, user_id, rest)
        if verb == "proposals":
            items = _proposals(b, user_id)
            return ("\n".join(["Proposed rails:"] + [_proposal_line(p) for p in items])
                    if items else "No rail proposals waiting.")
        if verb in ("accept", "dismiss"):
            return _decide_reply(b, user_id, rest, accept=(verb == "accept"), seat=seat)
        if verb in ("show", "on", "off", "drop", "edit", "grant", "revoke"):
            if not rest:
                return f"/rail {verb} needs a rail name.\n{USAGE}"
            from agents.task.goals.rails import find_rail
            row, why = find_rail(b, user_id, rest[0], include_dropped=(verb == "show"))
            if row is None:
                return why or "no such rail"
            if verb == "show":
                return render_show(b, user_id, row)
            if verb in ("on", "off"):
                return _switch_reply(b, user_id, row, on=(verb == "on"))
            if verb == "drop":
                return _drop_reply(b, user_id, row, rest[1:], seat)
            if verb == "edit":
                return _edit_reply(b, user_id, row, rest[1:])
            if verb == "grant":
                return _grant_reply(b, user_id, row, rest[1:], seat)
            return _revoke_reply(b, user_id, row, rest[1:], seat)
    except ValueError as e:
        return f"Refused: {e}"
    except Exception as e:  # an unreadable store is not an empty one
        logger.warning("/rail %s failed: %s", verb, e, exc_info=True)
        return f"/rail {verb}: unavailable({type(e).__name__}: {e})"
    return f"Unknown /rail verb '{verb}'.\n{USAGE}"


def _templates_reply() -> str:
    from agents.task.goals.rail_templates import TEMPLATES
    lines = ["Templates (nothing runs until you create one):"]
    for t in TEMPLATES:
        slots = " ".join(f"{s['name']}={s.get('default') or '…'}" for s in t["slots"])
        lines.append(f"• {t['key']} — {t['description']}\n   /rail new {t['key']} {slots}")
    return "\n".join(lines)


def _kv(rest: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for w in rest:
        if "=" in w:
            k, v = w.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _new_reply(board: Any, user_id: str, rest: List[str], seat: str) -> str:
    from agents.task.goals import rails as R
    from agents.task.goals.rail_templates import render, template_keys
    if not rest:
        return "Name a template: " + ", ".join(template_keys()) + " (see /rail templates)"
    key = rest[0].lower()
    values = _kv(rest[1:])
    name = values.pop("name", "")
    title, body, rec = render(key, values)
    if name:
        rec["name"] = name
    row = R.create_rail(board, user_id, title=title, body=body, recurrence=rec,
                        created_by=seat)
    return (f"✅ Rail {R.rail_name(row)} is ON — {rec['schedule']}. The first cycle seeds "
            f"on the next rails tick. /rail show {R.rail_name(row)} · /rail off "
            f"{R.rail_name(row)}")


def _adopt_reply(board: Any, user_id: str, rest: List[str]) -> str:
    from agents.task.goals import rails as R
    if len(rest) < 2:
        return "Usage: /rail adopt <objective id> <schedule>  e.g. every 24h"
    ref, schedule = rest[0], " ".join(rest[1:])
    obj = board.get(ref, user_id=user_id)
    if obj is None:
        hits = [o for o in board.objectives(user_id=user_id) if o.id.startswith(ref)]
        obj = hits[0] if len(hits) == 1 else None
    if obj is None or obj.kind != "objective":
        return f"No objective '{ref}'."
    if R.is_rail(obj):
        return f"{R.rail_name(obj)} is already a rail."
    rec = R.validate_recurrence({
        "name": R.slug(obj.title), "schedule": schedule,
        "legs": [{"title": obj.title, "body": obj.body or obj.title}], "max_live": 1})
    board.merge_payload(obj.id, {R.RECURRENCE_KEY: rec})
    return f"✅ Adopted '{obj.title[:80]}' as rail {rec['name']} — {rec['schedule']}."


def _switch_reply(board: Any, user_id: str, row: Any, *, on: bool) -> str:
    from agents.task.goals.rails import rail_name
    board.set_objective_status(row.id, "active" if on else "paused", user_id=user_id)
    if on:
        return f"✅ {rail_name(row)} is ON. It seeds when its schedule is due."
    return (f"⏸ {rail_name(row)} is OFF. Live legs finish; nothing new seeds. "
            f"/rail on {rail_name(row)} to resume.")


def _drop_reply(board: Any, user_id: str, row: Any, rest: List[str], seat: str) -> str:
    """036 Q4 (owner decision): asks once — by default live legs FINISH (the rail
    is off and cannot re-seed); ``--cancel-live`` also cancels them."""
    from agents.task.goals import rails as R
    from core.goal_suppressions import SCOPE_STREAM, suppress
    from core.goal_vocab import LIVE_STATUSES
    from core.tool_grants import revoke_all
    cancel_live = any(w in ("--cancel-live", "cancel-live") for w in rest)
    live = [g for g in R.rail_goals(board, user_id, row.id) if g.status in LIVE_STATUSES]
    board.set_objective_status(row.id, "dropped", user_id=user_id)
    # Latched: a dropped rail is never re-offered or re-seeded under its id.
    suppress(board.db_path, user_id=user_id, scope=SCOPE_STREAM, value=row.id,
             label=R.rail_name(row), reason="rail dropped by the owner")
    n_grants = revoke_all(board.db_path, user_id=user_id, rail_id=row.id,
                          revoked_by=f"{user_id}@{seat}")
    cancelled = 0
    if cancel_live:
        for g in live:
            if board.cancel(g.id, user_id=user_id):
                cancelled += 1
    msg = [f"🗑 Dropped rail {R.rail_name(row)}"
           + (f"; {n_grants} grant(s) revoked" if n_grants else "") + "."]
    if cancel_live:
        msg.append(f"Cancelled {cancelled} live leg(s).")
    elif live:
        msg.append(f"{len(live)} live leg(s) will finish (the rail cannot re-seed). To "
                   f"stop them too: /rail drop {R.rail_name(row)} --cancel-live")
    return " ".join(msg)


def _edit_reply(board: Any, user_id: str, row: Any, rest: List[str]) -> str:
    from agents.task.goals import rails as R
    if len(rest) < 2:
        return ("Usage: /rail edit <name> body \"…\" | schedule \"every 24h\" | max <n> | "
                "priority <n> | title \"…\"")
    field, value = rest[0].lower(), " ".join(rest[1:]).strip()
    rec = dict(R.recurrence_of(row) or {})
    if field == "schedule":
        R.update_recurrence(board, row, {"schedule": value})
    elif field in ("max", "max_live"):
        R.update_recurrence(board, row, {"max_live": value})
    elif field == "body":
        legs = list(rec.get("legs") or [])
        if len(legs) != 1:
            return (f"{R.rail_name(row)} has {len(legs)} legs — edit a leg with "
                    f"body.<n> \"…\" (e.g. body.2)")
        legs[0] = dict(legs[0], body=value)
        R.update_recurrence(board, row, {"legs": legs})
    elif field.startswith("body."):
        legs = list(rec.get("legs") or [])
        try:
            idx = int(field.split(".", 1)[1]) - 1
            legs[idx] = dict(legs[idx], body=value)
        except (ValueError, IndexError):
            return f"No leg {field.split('.', 1)[1]} on {R.rail_name(row)}."
        R.update_recurrence(board, row, {"legs": legs})
    elif field == "priority":
        board.update_fields(row.id, user_id=user_id, priority=int(value))
    elif field == "title":
        board.update_fields(row.id, user_id=user_id, title=value[:200])
    else:
        return f"Cannot edit '{field}' (body, body.<n>, schedule, max, priority, title)."
    return f"✅ {R.rail_name(row)}: {field} updated. It applies from the next seed."


def _grant_reply(board: Any, user_id: str, row: Any, rest: List[str], seat: str) -> str:
    from agents.task.goals.rails import base_tools, rail_name, recurrence_of
    from core.tool_grants import (gated_ids, grant_rail_tool, grantable_tool, is_money,
                                  money_armed)
    if not rest:
        return f"Usage: /rail grant {rail_name(row)} <tool>"
    tool = rest[0].strip().lower()
    confirmed = len(rest) > 1 and rest[1].lower() in ("confirm", "yes")
    if not grantable_tool(tool):
        return f"Unknown tool '{tool}'. A grant names a tool id, e.g. defi_trade, shell, publish."
    if tool not in gated_ids() or tool in base_tools(recurrence_of(row) or {}):
        return (f"{tool} needs no grant — {rail_name(row)}'s legs already carry it (or it "
                f"is not a gated tool).")
    if not confirmed:
        warn = ""
        if is_money(tool):
            warn = (" It moves money: every spend still runs through the wallet caps and "
                    "the owner queue.")
            if not money_armed():
                warn += " The money regime is NOT armed, so it stays inert until it is."
        return (f"⚠️ Grant {tool} to every future leg of rail {rail_name(row)}?{warn}\n"
                f"To confirm, send: /rail grant {rail_name(row)} {tool} confirm")
    g = grant_rail_tool(board.db_path, user_id=user_id, rail_id=row.id, tool_id=tool,
                        granted_by=f"{user_id}@{seat}")
    inert = is_money(tool) and not money_armed()
    return (f"🔑 Granted {g.tool_id} on {rail_name(row)} ({g.granted_by}, audited)."
            + (" INERT until the money regime is armed." if inert else "")
            + f" Revoke any time: /rail revoke {rail_name(row)} {g.tool_id}")


def _revoke_reply(board: Any, user_id: str, row: Any, rest: List[str], seat: str) -> str:
    from agents.task.goals.rails import rail_name
    from core.tool_grants import revoke_rail_tool
    if not rest:
        return f"Usage: /rail revoke {rail_name(row)} <tool>"
    tool = rest[0].strip().lower()
    if revoke_rail_tool(board.db_path, user_id=user_id, rail_id=row.id, tool_id=tool,
                        revoked_by=f"{user_id}@{seat}"):
        return f"✅ Revoked {tool} on {rail_name(row)}. The next seed runs without it."
    return f"{rail_name(row)} has no grant for {tool}."


def _proposals(board: Any, user_id: str) -> list:
    from agents.task.goals import rail_proposals
    return rail_proposals.pending(getattr(board, "db_path", None), user_id=user_id)


def _proposal_line(p) -> str:
    needs = f" · needs grant: {', '.join(p.needs)}" if p.needs else ""
    return (f"• {p.key} — {p.recurrence.get('schedule')} · from {p.source}{needs}\n"
            f"   /rail accept {p.key}   /rail dismiss {p.key}")


def _decide_reply(board: Any, user_id: str, rest: List[str], *, accept: bool,
                  seat: str) -> str:
    from agents.task.goals import rail_proposals
    from agents.task.goals.rails import create_rail
    if not rest:
        return "Name a proposal key (see /rail proposals)."
    key = rest[0].strip().lower()
    p = rail_proposals.get(board.db_path, user_id=user_id, key=key)
    if p is None or p.status != rail_proposals.PENDING:
        return f"No pending proposal '{key}'."
    if not accept:
        rail_proposals.decide(board.db_path, user_id=user_id, key=key, accept=False)
        return f"Dismissed '{key}'. It will not be offered again."
    row = create_rail(board, user_id, title=p.title, body=p.body, recurrence=p.recurrence,
                      created_by=f"proposal:{p.source}@{seat}")
    rail_proposals.decide(board.db_path, user_id=user_id, key=key, accept=True)
    needs = (f" It named {', '.join(p.needs)} — nothing is granted; /rail grant "
             f"{p.key} <tool> if you want that.") if p.needs else ""
    return f"✅ Rail {p.key} is ON — {p.recurrence.get('schedule')}.{needs}"


def _grants_reply(board: Any, user_id: str) -> str:
    from agents.task.goals.rails import list_rails, rail_name
    from core.tool_grants import all_grants
    rows = all_grants(board.db_path, user_id=user_id)
    if not rows:
        return "No rail grants. Every rail runs on the ordinary goal toolset."
    names = {r.id: rail_name(r) for r in list_rails(board, user_id, include_dropped=True)}
    lines = ["Rail grants:"]
    for g in rows:
        state = " (PENDING)" if g.pending else ""
        lines.append(f"• {names.get(g.rail_id, g.rail_id[:8])}: {g.tool_id}{state} — "
                     f"{g.granted_by} at {_when(g.granted_at)}")
    return "\n".join(lines)


__all__ = ["USAGE", "rail_reply", "render_list", "render_show"]
