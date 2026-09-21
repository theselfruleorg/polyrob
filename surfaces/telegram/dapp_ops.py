"""`/dapp` — the wallet sessions a web page holds, and how to cut one off (E9).

A ``dapp_connect`` arms this agent's wallet inside a browser page under a
declared envelope (chain, per-transaction ceiling, whole-session budget) and
records every transaction it signed and every request it refused. Until now the
only reader was the agent's own ``dapp_status``, on the session that armed it —
so the owner had no way to ask "what is my wallet connected to right now?" and
no way at all to revoke one.

Reads the DURABLE store (``core/dapp_session_store.py``), which is the only
record that survives a restart.

⚠️ **What `revoke` really does, said plainly.** The store never RESUMES a
wallet — a live ``WalletBridge`` holds its own in-memory envelope, and a
spend is authorised against that object, not against this row. Marking the row
revoked is therefore durable and immediate for every FUTURE read, and it does
NOT reach into a bridge that is already armed inside a running session in
another process. The reply says so and names the certain remedy (`/pause` the
agent, or end that session) rather than reporting a kill it cannot guarantee.

Shared: the REPL and the CLI import :func:`dapp_reply`.
"""
from __future__ import annotations

import logging
import time
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

#: Rows one chat listing renders before collapsing the rest into a count.
_LIST_LIMIT = 10

USAGE = (
    "Usage: /dapp list\n"
    "/dapp revoke <session-id>\n\n"
    "`list` shows every page my wallet has been armed for, with what it spent "
    "and what it refused. `revoke` retires that authorization."
)


def _fmt_usd(value: Any) -> str:
    try:
        return f"${float(value):.4f}"
    except (TypeError, ValueError):
        return "unknown"


def _fmt_row(row: Any) -> str:
    env = getattr(row, "envelope", None) or {}
    origin = env.get("origin") or env.get("url") or "(origin not recorded)"
    chain = env.get("chain") or "?"
    spent = _fmt_usd(env.get("spent_usd"))
    budget = _fmt_usd(env.get("budget_usd") or env.get("session_budget_usd"))
    sent = env.get("sent")
    refused = env.get("refused")
    n_sent = len(sent) if isinstance(sent, list) else "?"
    n_refused = len(refused) if isinstance(refused, list) else "?"
    state = "REVOKED" if getattr(row, "revoked", False) else "live"
    when = time.strftime("%m-%d %H:%M",
                         time.gmtime(float(getattr(row, "updated_at", 0) or 0)))
    return (f"• `{getattr(row, 'session_id', '?')}` [{state}] {origin}\n"
            f"   chain {chain} · spent {spent} of {budget} · "
            f"{n_sent} sent, {n_refused} refused · updated {when}Z")


#: What both verbs answer when no dapp-session record exists on this box.
_NO_RECORD = (
    "No web page has ever been armed with my wallet on this box — there is no "
    "dapp-session record here at all.\n"
    "⚠️ This is the DURABLE record. A bridge armed inside a session that has "
    "not written a row yet would not appear."
)


def _open_store():
    """The durable dapp-session store, or ``None`` when there is none.

    ⚠️ A READ never CREATES the store. ``get_dapp_session_store`` constructs a
    ``DappSessionStore``, whose ``__init__`` runs its DDL — so `/dapp` on a box
    that has never armed a page MINTED ``dapp_sessions.db`` just to answer
    "nothing is connected", and every later "the file exists" check (backups,
    the db manifest, an operator's `ls`) then read a store nobody asked for.
    Same guard, same reason, as ``cli/commands/wallet.py::_dapp_store``.
    """
    import os as _os

    from core.dapp_session_store import (
        default_dapp_session_store_path, get_dapp_session_store,
    )
    path = default_dapp_session_store_path()
    if not _os.path.exists(path):
        return None
    return get_dapp_session_store(path)


def dapp_reply(user_id: Optional[str], args: List[str]) -> str:
    """``/dapp list|revoke <id>`` — one chat-ready string. Never raises."""
    if not user_id:
        return "Only the owner can use /dapp."
    tokens = [str(a) for a in (args or []) if str(a).strip()]
    verb = (tokens[0].lower() if tokens else "list")
    rest = tokens[1:]

    try:
        store = _open_store()
    except Exception as exc:
        logger.warning("dapp session store unavailable", exc_info=True)
        return (f"I could not open the dapp-session record ({type(exc).__name__}: "
                f"{str(exc)[:100]}). That is UNKNOWN, not 'nothing is connected'.")
    if store is None:
        # Right for BOTH verbs: with no record there is also nothing to revoke,
        # and saying so beats materialising a database to answer "none".
        return _NO_RECORD

    if verb in ("list", "ls", ""):
        try:
            rows = store.list_for_tenant(user_id)
        except Exception as exc:
            logger.warning("dapp session list failed", exc_info=True)
            return (f"The dapp-session record is unreadable "
                    f"({type(exc).__name__}: {str(exc)[:100]}) — UNKNOWN, not "
                    f"'nothing is connected'.")
        if not rows:
            return ("No web page has been armed with my wallet on this box.\n"
                    "⚠️ This is the DURABLE record. A bridge armed inside a "
                    "session that has not written a row yet would not appear.")
        shown = rows[:_LIST_LIMIT]
        lines = [f"{len(rows)} dapp session(s):"] + [_fmt_row(r) for r in shown]
        if len(rows) > len(shown):
            lines.append(f"(+{len(rows) - len(shown)} more)")
        lines.append("Cut one off: /dapp revoke <session-id>")
        return "\n".join(lines)

    if verb not in ("revoke", "disconnect", "kill"):
        return f"Unknown /dapp verb {verb!r}.\n{USAGE}"

    if not rest:
        return "Usage: /dapp revoke <session-id> (see /dapp list)"
    session_id = rest[0]
    try:
        row = store.get(session_id, user_id)
    except Exception as exc:
        logger.warning("dapp session read failed", exc_info=True)
        return (f"I could not read that session ({type(exc).__name__}: "
                f"{str(exc)[:100]}) — I have NOT revoked anything.")
    if row is None:
        return (f"No dapp session `{session_id}` under your tenant — "
                f"see /dapp list.")
    if getattr(row, "revoked", False):
        return f"`{session_id}` was already revoked. Nothing changed."
    try:
        store.mark_revoked(session_id)
        after = store.get(session_id, user_id)
    except Exception as exc:
        logger.warning("dapp session revoke failed", exc_info=True)
        return (f"I could not revoke `{session_id}` ({type(exc).__name__}: "
                f"{str(exc)[:100]}). Assume it is STILL armed.")
    if after is None or not getattr(after, "revoked", False):
        return (f"I wrote the revocation for `{session_id}` and could not read "
                f"it back as revoked. Assume it is STILL armed — stop me with "
                f"/pause everything.")
    return (f"✅ `{session_id}` revoked in my durable record.\n"
            "⚠️ A bridge that is ALREADY armed inside a running session holds "
            "its own copy of the authorization, so this binds every future "
            "read and may not stop one mid-flight. To be certain right now: "
            "/pause everything, or /cancel that session.")


__all__ = ["USAGE", "dapp_reply"]
