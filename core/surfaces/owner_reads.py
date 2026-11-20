"""Owner read replies every chat seat renders: /missed, /journey, /mode, /rules,
/contacts, /thread, /why and the health header the read verbs lead with.

Moved below the seats (proposal 125 S14) from ``surfaces/telegram/status_ops.py``
and ``owner_ops.py``: Telegram, the console chat
box and the REPL import this module, so one verb gives one text. Core reads only.
"""
import asyncio
import logging
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


async def health_header(user_id: str, data_dir: str, task_agent: Any = None,
                         limit: int = 4) -> list:
    """The mandatory health block other read verbs (/mode, /recap, /goals)
    prepend — a degraded instance renders first on EVERY seat. Cheap path (no
    money, no balances). Never raises: a builder failure renders as its own
    'unavailable' line rather than silently producing no header."""
    try:
        from core.status_snapshot import build_status_snapshot
        from core.status_render import render_health_lines
        from core.status_render import pause_headline
        snap = await asyncio.to_thread(
            build_status_snapshot, user_id, data_dir=data_dir, task_agent=task_agent,
            include_money=False)
        # 031: the pause state leads every seat, before health.
        return [pause_headline(snap)] + render_health_lines(snap, prefix="• ", limit=limit,
                                                            brief=True)
    except Exception as e:
        logger.warning("health header failed: %s", e, exc_info=True)
        return [f"Health: unavailable ({type(e).__name__}: {str(e)[:120]})"]


def missed_reply(user_id: str, data_dir: str, args: list) -> str:
    """`/missed [n]` — the owner notices the delivery rail could not send
    live (durable ``owner_notice`` rows the rail writes instead of dropping
    the text): suppressed by the daily cap, held by an owner pause, or
    undelivered (no live sink / send failed) — newest first. 2026-08-28: 91
    of 92 notices in 24h were capped and nothing let the owner read them; A7
    (2026-09-14) widened this to every marker (it used to match the cap
    marker only, so a paused/undelivered notice was unreadable). A thin
    renderer over ``core.surfaces.missed.missed_notices`` — CLI/REPL reuse
    the same query."""
    try:
        n = max(1, min(20, int(args[0]))) if args else 5
    except (TypeError, ValueError):
        return "Usage: /missed [n] (1-20, default 5)"
    try:
        from core.surfaces.missed import missed_notices
        rows = missed_notices(user_id, data_dir, n)
    except Exception as e:
        return f"Missed messages: unavailable ({type(e).__name__}: {str(e)[:120]})"
    if not rows:
        return "No undelivered messages on record."
    # D36: the SHARED renderer (`core.surfaces.missed.format_notice_lines`),
    # which WRAPS. This seat used to hard-clip every notice at 240 characters —
    # in the one verb that exists so the owner can recover text he never
    # received, which makes the clip a second suppression of the same message.
    from core.surfaces.missed import format_notice_lines
    lines = [f"Last {len(rows)} undelivered message(s), newest first:"]
    for r in rows:
        lines.extend(format_notice_lines(
            float(r.get("ts") or 0), str(r.get("kind") or "capped"),
            str(r.get("text") or "").strip(), gutter="• ", width=MISSED_WIDTH))
    from core.surfaces.missed import cap_caused
    if cap_caused(rows):
        lines.append("Raise the daily limit: /config set delivery.daily_cap N")
    return "\n".join(lines)


#: A chat bubble re-wraps proportional text, so the terminal's 80 columns are
#: the wrong measure here. Narrow enough that one notice line stays one line.
MISSED_WIDTH = 60


async def journey_reply(user_id: str, data_dir: str, task_agent: Any, args: list) -> str:
    """`/journey [window]` on every seat (113 W4 item 3, D-2): the health
    header, then the recap. The chat apps and the terminal both call this, so
    one verb gives one text."""
    header = await health_header(user_id, data_dir, task_agent, limit=3)
    from core.recap import recap_reply
    return "\n".join(header) + "\n" + recap_reply(user_id, data_dir, args[0] if args else "24h",
                                                 more_tenant=user_id)


def mode_reply(seat: str = "telegram", *, full: bool = False) -> str:
    """`/mode` — the four answers a person needs (104 §2); `/mode all` adds the card.

    The seat decides the first line and the "to change" line: a write the terminal may do
    must not read "needs the server" there. A level name writes that level (104 §4)."""
    try:
        from core.config_policy.posture_card import build_answers, render_posture_card
        lines = list(build_answers(seat))
        if full:
            lines += ["", "All settings behind this:"] + render_posture_card(prefix="• ")
        else:
            lines += ["", "/mode all shows every setting behind this."]
    except Exception:
        logger.debug("/mode answers failed", exc_info=True)
        return ("I could not read my own settings. Ask me again in a "
                "moment, or run `polyrob autonomy status` on my box.")
    return "\n".join(lines)


def _numbered(text: str, start: int = 1) -> Tuple[List[str], int]:
    """Rule lines numbered in order; a heading stays as a plain label."""
    out, n = [], start
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            out.append(line.lstrip("#").strip() + ":")
            continue
        body = line[2:].strip() if line.startswith(("- ", "* ")) else line
        out.append(f"{n}. {body}")
        n += 1
    return out, n


def rules_reply(user_id: str, data_dir: str, instance_id: str) -> str:
    """`/rules` — what I hold myself to: the rules you gave me (owner.md, the
    ACTIVE part, the same text my sessions load) and my operating contract
    (contract.md). Numbered, read only; changing one goes through the agent
    and your approval (/pending)."""
    from core.instance import load_contract_doc, load_owner_doc
    out = []
    try:
        owner = load_owner_doc(data_dir, user_id, instance_id)
    except Exception as exc:   # noqa: BLE001 — the loader never raises; say so if it does
        owner = f"[unreadable: {type(exc).__name__}]"
    try:
        contract = load_contract_doc(data_dir, user_id, instance_id)
    except Exception as exc:   # noqa: BLE001
        contract = f"[unreadable: {type(exc).__name__}]"
    out.append("Your rules for me (owner.md):")
    if owner.strip():
        lines, _n = _numbered(owner)
        out += lines or ["(none written)"]
    else:
        out.append("(none written)")
    out.append("")
    out.append("My operating contract (contract.md):")
    if contract.strip():
        lines, _n = _numbered(contract)
        out += lines or ["(none written)"]
    else:
        out.append("(none written)")
    out.append("")
    out.append("Read only. To change a rule, tell me — the change waits for your "
               "approval in /pending.")
    return "\n".join(out)


# --- /contacts, /thread, /why (E10, 061, O10): the read lives in core; these parse the words.

#: Conversations one /contacts listing renders, messages one transcript shows, and
#: the most characters (a phone bubble, not a mail archive).
_CONTACTS_LIST_LIMIT = 20
_CONTACTS_HISTORY_LIMIT = 10
_CONTACTS_MAX_CHARS = 2500

CONTACTS_USAGE = (
    "Usage: /contacts                      — everyone I have written to\n"
    "/contacts <surface> <address>         — the transcript with one of them\n\n"
    "e.g. /contacts email someone@example.com\n"
    "Read-only. Approve or reject a person from /pending."
)


def contacts_reply(user_id: Optional[str], data_dir: str, args: List[str],
                   container: Any = None) -> str:
    """``/contacts [<surface> <address>]`` — one chat-ready string. Never raises.

    Read-only: approving a correspondent stays on `/pending`.
    ``core.surfaces.contacts_view.contacts_text`` holds the read, the contact state
    and the untrusted frame (a transcript is a third party's words; the agent's
    ``contact_history`` prints the same text)."""
    tokens = [str(a) for a in (args or []) if str(a).strip()]
    if user_id and len(tokens) == 1:
        return ("Give me both halves: /contacts <surface> <address> "
                "(e.g. /contacts email someone@example.com), or bare "
                "/contacts for the listing.")
    from core.surfaces.contacts_view import contacts_text
    surface, address = (tokens[0], tokens[1]) if len(tokens) >= 2 else ("", "")
    return contacts_text(user_id, data_dir, surface, address, container=container,
                         list_limit=_CONTACTS_LIST_LIMIT,
                         history_limit=_CONTACTS_HISTORY_LIMIT,
                         max_chars=_CONTACTS_MAX_CHARS)


HISTORY_USAGE = ("/thread [n | <hours>h] — my conversation with you across every session "
                 "and rail (default: last 20 lines). Example: /thread 6h")

_THREAD_DEFAULT_ROWS = 20
_THREAD_MAX_ROWS = 60
_THREAD_MAX_CHARS = 3500


def _thread_window(args: List[str]) -> tuple:
    """``(limit, hours)`` from ``[n]`` or ``[<h>h]``."""
    tok = (args[0] if args else "").strip().lower()
    if not tok:
        return _THREAD_DEFAULT_ROWS, None
    if tok.endswith("h") and tok[:-1].isdigit():
        return _THREAD_MAX_ROWS, max(1, min(int(tok[:-1]), 24 * 14))
    if tok.isdigit():
        return max(1, min(int(tok), _THREAD_MAX_ROWS)), None
    return _THREAD_DEFAULT_ROWS, None


def history_reply(user_id: Optional[str], data_dir: str, args: List[str],
                  container: Any = None) -> str:
    """``/thread`` — the ONE owner transcript, every rail, newest last. Never raises.

    A READ never creates the store; an unreadable one is NAMED."""
    if not user_id:
        return "Only the owner can read our conversation."
    limit, hours = _thread_window([str(a) for a in (args or []) if str(a).strip()])
    try:
        from core.surfaces.owner_thread import render_transcript, thread_recent
        rows = thread_recent(container, str(user_id), limit=limit, hours=hours,
                             data_dir=data_dir)
    except Exception as exc:
        return (f"our conversation: unavailable({type(exc).__name__}: {exc}) — that is "
                f"UNKNOWN, not an empty history.")
    if rows is None:
        return ("no conversation recorded yet — the thread starts with the next line "
                "either of us sends.")
    if not rows:
        window = f"in the last {hours}h" if hours else "yet"
        return f"no lines {window}."
    head = (f"our conversation — last {len(rows)} line(s)"
            + (f" in {hours}h" if hours else "") + ", every session and rail:")
    body = render_transcript(rows)
    if len(body) > _THREAD_MAX_CHARS:
        body = "…" + body[-_THREAD_MAX_CHARS:]
    return head + "\n" + body


WHY_USAGE = (
    "Usage: /why [n]  — the last n refusals (default 5, at most 20)\n"
    "Each line: when, which ability, the gate's reason, word for word."
)


def why_reply(user_id: Optional[str], data_dir: str, args: List[str]) -> str:
    """``/why [n]`` — the last gate refusals, each reason verbatim
    (``core.security_digest.refusals_reply``; the agent's
    ``agent_status(view="refusals")`` prints the same)."""
    from core.security_digest import REFUSALS_DEFAULT_N, refusals_reply
    n = REFUSALS_DEFAULT_N
    if args:
        try:
            n = int(args[0])
        except ValueError:
            return WHY_USAGE
        if n < 1:
            return WHY_USAGE
    return refusals_reply(user_id or "", data_dir, n)
