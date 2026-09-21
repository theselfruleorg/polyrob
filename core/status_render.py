"""Renderers over :mod:`core.status_snapshot` — one vocabulary on every seat.

Every owner-facing and agent-facing status surface renders from these
functions, so ``/status`` (Telegram), ``polyrob doctor``, ``polyrob autonomy
status``, the webview ``/system`` page, the daily digest and the agent's own
``agent_status`` action cannot disagree about whether the instance is healthy.

The health block ALWAYS comes first and is never empty: it says ``DEGRADED``
with the ranked issues, ``PARTIAL`` with what could not be verified, or ``OK``
with the list of what was actually checked (so "OK" is a claim with evidence,
not an absence of lines).
"""
from __future__ import annotations

import time
from typing import List, Optional

from core.status_snapshot import (
    OVERALL_DEGRADED, OVERALL_OK, OVERALL_PARTIAL, SECTION_ORDER, SEVERITY_CRIT,
    STATE_DEGRADED, STATE_UNAVAILABLE, StatusSnapshot,
)

_CHECKED = ("credit sentinel", "live provider", "open asks", "blocked goals",
            "pending approvals", "objective budgets", "delivery cap", "loop heartbeats",
            "owner pause", "surface circuits", "tool timeouts")

_SECTION_TITLES = {
    "session": "Session",
    "providers": "Providers",
    "work": "Goals",
    "approvals": "Approvals",
    "loops": "Loops",
    "delivery": "Delivery",
    "posture": "Posture",
    "security": "Security",
    "identity": "Identity",
    "apps": "Apps",
    "groups": "Groups",
    "room_actions": "Paid room actions",
    "creations": "Made",
    "collectibles": "Collectibles",
    "liquidity": "Liquidity",
    "wallet": "Wallet",
    "money": "Money",
    "economics": "Economics",
}


def health_headline(snap: StatusSnapshot) -> str:
    n_crit = sum(1 for h in snap.health if h.severity == SEVERITY_CRIT)
    if snap.overall == OVERALL_DEGRADED:
        head = f"Health: DEGRADED — {len(snap.health)} issue(s)"
        if n_crit:
            head += f", {n_crit} critical"
        if snap.unavailable_sources:
            head += f"; {len(snap.unavailable_sources)} source(s) unreadable"
        return head
    if snap.overall == OVERALL_PARTIAL:
        return ("Health: PARTIAL — no issue found, but could not verify: "
                + "; ".join(snap.unavailable_sources))
    return f"Health: OK — no issue found (checked: {', '.join(_CHECKED)})"


def render_health_lines(snap: StatusSnapshot, *, prefix: str = "• ",
                        limit: Optional[int] = None) -> List[str]:
    """The mandatory first block: headline + one line per ranked issue."""
    lines = [health_headline(snap)]
    items = snap.health if limit is None else snap.health[:max(1, int(limit))]
    for h in items:
        mark = "⛔" if h.severity == SEVERITY_CRIT else "⚠"
        line = f"{prefix}{mark} {h.text}"
        if h.remedy:
            line += f" → {h.remedy}"
        lines.append(line)
    hidden = len(snap.health) - len(items)
    if hidden > 0:
        lines.append(f"{prefix}… +{hidden} more (full list: /status on the console, "
                     "`polyrob autonomy status`)")
    if snap.overall == OVERALL_DEGRADED and snap.unavailable_sources:
        lines.append(f"{prefix}could not verify: " + "; ".join(snap.unavailable_sources))
    return lines


def render_section_lines(snap: StatusSnapshot, name: str, *, prefix: str = "• ") -> List[str]:
    """One section, honestly: ``<Title>: unavailable (<reason>)`` when it could
    not be computed — never a missing line, never a zero."""
    sec = snap.sections[name]
    title = _SECTION_TITLES.get(name, name)
    if sec.state == STATE_UNAVAILABLE:
        return [f"{prefix}{title}: unavailable ({sec.reason})"]
    if not sec.lines:
        return [f"{prefix}{title}: (no data)"]
    if name == "posture":
        return [f"{prefix}Posture:"] + [f"{prefix}  {ln}" for ln in sec.lines]
    first = f"{prefix}{title}: {sec.lines[0]}"
    rest = [f"{prefix}  {ln}" for ln in sec.lines[1:]]
    return [first] + rest


def _hhmm_ts(ts) -> str:
    try:
        return time.strftime("%H:%M UTC", time.gmtime(float(ts)))
    except (TypeError, ValueError, OverflowError):
        return "?"


#: Chat-seat defaults; a non-chat seat passes its own verbs.
CHAT_RESUME_HINT = "/resume"
CHAT_PAUSE_HINT = "/pause"


def pause_headline_from(p: dict, live: Optional[dict] = None, *,
                        resume_hint: str = CHAT_RESUME_HINT,
                        pause_hint: str = CHAT_PAUSE_HINT) -> str:
    """One line from a pause dict (``loops.data['pause']`` shape) + live actors.
    Without ``live`` (a seat that builds no snapshot) the running line carries
    no actor counts rather than question marks."""
    if p.get("paused"):
        scopes = p.get("scopes") or ["all"]
        words = "everything" if "all" in scopes else ", ".join(scopes)
        who = f"{p.get('set_by') or p.get('source') or '?'} via {p.get('via') or '-'}"
        until = f", auto-resumes {_hhmm_ts(p.get('until'))}" if p.get("until") else ""
        return (f"⏸ PAUSED ({words}) since {_hhmm_ts(p.get('since'))} by {who}{until} "
                f"— {resume_hint} to continue")
    if not live:
        return f"▶ RUNNING — {pause_hint} to stop"
    alive = live.get("loops_alive") or {}
    n_alive = sum(1 for v in alive.values() if v)
    rg = live.get("running_goals")
    rc = live.get("running_cron")
    return (f"▶ RUNNING — {'?' if rg is None else rg} goal run(s), "
            f"{'?' if rc is None else rc} cron run(s), loops alive {n_alive}/{len(alive) or '?'} "
            f"— {pause_hint} to stop")


def pause_headline(snap: StatusSnapshot, *, resume_hint: str = CHAT_RESUME_HINT,
                   pause_hint: str = CHAT_PAUSE_HINT) -> str:
    """The FIRST line of every status seat (031): the pause state, verified from
    the ONE record — never 'running' by omission. The seat passes its own verbs."""
    loops = snap.sections.get("loops")
    if loops is not None and loops.available:
        return pause_headline_from(loops.data.get("pause") or {},
                                   loops.data.get("live_actors") or {},
                                   resume_hint=resume_hint, pause_hint=pause_hint)
    # The pause record is a global fact (not tenant-scoped, never created by a
    # read): when the loops section is unreadable (no tenant, a missing store),
    # read the record directly rather than reporting the pause as unknown.
    try:
        from core.autonomy_control import read_state
        return pause_headline_from(read_state().to_dict(), resume_hint=resume_hint,
                                   pause_hint=pause_hint)
    except Exception as e:
        reason = getattr(loops, "reason", None) or f"{type(e).__name__}: {str(e)[:80]}"
        return f"autonomy: unavailable ({reason})"


def render_status_lines(snap: StatusSnapshot, *, prefix: str = "• ",
                        sections: Optional[List[str]] = None,
                        health_limit: Optional[int] = None,
                        resume_hint: str = CHAT_RESUME_HINT,
                        pause_hint: str = CHAT_PAUSE_HINT) -> List[str]:
    """The full snapshot: the pause line, health, then every section in SECTION_ORDER."""
    out = ([pause_headline(snap, resume_hint=resume_hint, pause_hint=pause_hint)]
           + render_health_lines(snap, prefix=prefix, limit=health_limit))
    for name in (sections or SECTION_ORDER):
        out.extend(render_section_lines(snap, name, prefix=prefix))
    return out


def render_status_text(snap: StatusSnapshot, *, title: str = "Status:",
                       prefix: str = "• ", health_limit: Optional[int] = None,
                       resume_hint: str = CHAT_RESUME_HINT,
                       pause_hint: str = CHAT_PAUSE_HINT) -> str:
    return "\n".join([title] + render_status_lines(snap, prefix=prefix,
                                                   resume_hint=resume_hint, pause_hint=pause_hint,
                                                   health_limit=health_limit))


def wallet_note_lines(snap: StatusSnapshot) -> List[str]:
    """The agent's own balances, for the per-turn note (039 Unit D).

    The agent could always LOOK (`defi_data.portfolio`). What it could not do was
    KNOW without being asked to check — so "how much ETH do I have?" was answered
    from whatever happened to be in context. On 2026-08-28 that produced a
    published claim that the book was flat over three open positions.

    Gated `WALLET_CONTEXT_VISIBLE` (default ON). Every figure is cache-backed, so
    this costs the turn nothing; an unread chain says `unknown` rather than
    disappearing, because a missing row reads as an empty wallet.
    """
    from core.env import bool_env
    if not bool_env("WALLET_CONTEXT_VISIBLE", True):
        return []
    sec = snap.sections.get("wallet")
    if sec is None:
        return []
    if sec.state == STATE_UNAVAILABLE:
        return [f"- wallet: unavailable ({sec.reason}) — say so; do not state a "
                f"balance you have not read"]
    out = [f"- {line.strip()}" if i == 0 else f"  {line.strip()}"
           for i, line in enumerate(sec.lines)]
    if sec.state == STATE_DEGRADED:
        out.append("  ⚠ treat any `unknown` above as UNKNOWN, never as zero")
    return out


def render_agent_health_note(snap: StatusSnapshot, *, max_items: int = 8) -> str:
    """Compact, per-turn note for the AGENT (injected as a control message):
    what is degraded right now, so "how are you doing?" is answered from the
    same facts the owner's /status shows — never from stale context."""
    lines = [f"Live health as of {_stamp(snap)} (same source as the owner's /status):"]
    lines.append(health_headline(snap))
    # The agent's own verb is the action for PAUSE. There is no agent resume verb
    # (034 §11.2) — only an owner seat lifts a pause, so the note names /resume and
    # the agent relays that instead of trying a call that refuses.
    lines.append(pause_headline(snap, resume_hint="/resume (owner only)",
                                pause_hint="autonomy_control(pause)"))
    for h in snap.health[:max_items]:
        mark = "CRITICAL" if h.severity == SEVERITY_CRIT else "warn"
        lines.append(f"- [{mark}] {h.text}")
    if len(snap.health) > max_items:
        lines.append(f"- … +{len(snap.health) - max_items} more (call agent_status)")
    for name in ("providers", "work", "delivery"):
        sec = snap.sections.get(name)
        if sec is None:
            continue
        if sec.state == STATE_UNAVAILABLE:
            lines.append(f"- {name}: unavailable ({sec.reason})")
        elif sec.lines:
            lines.append(f"- {name}: {sec.lines[0]}")
    lines.extend(wallet_note_lines(snap))
    lines.append("If asked how things are going, state these facts first; do not claim "
                 "a clean state that this note contradicts.")
    return "\n".join(lines)


def _stamp(snap: StatusSnapshot) -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(snap.generated_at))
