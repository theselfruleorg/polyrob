"""The token-identity ask: "which contract is the real token?" as a TAP (W1).

When the identity gate (``tools/defi/identity_gate.py``) refuses a buy because
no candidate is trusted — two contracts claim one symbol and neither is ours,
the owner's target or the owner's pick; or an unverified token above the $5
unchecked ticket — the refusal used to tell the agent to ask the owner in chat.
Prose never became a binding (2026-09-25: "Pinned. Done in both places." was
false for the guard), and the only real remedy was a server CLI.

Now the gate raises ONE structured ask on the durable goal-board asks store
(``payload.ask_kind = "token_identity"``) — the SAME store tool approvals ride —
and ``tools/controller/approval_queue.all_pending`` lists each undecided
candidate as its own pending item, so every owner seat decides it with the
verbs and buttons it already has (``/pending``, ``/approve_p_…``, the console
Inbox):

* **Approve** a candidate = trust it: an ``owner_approved`` binding
  (``core.wallet.token_trust.trust``); the other candidates' held positions are
  quarantined and the ask closes.
* **Reject** a candidate = NOT trusted: the owner's rejection is recorded and a
  held position at that address is quarantined
  (``core.wallet.token_trust.untrust``). The ask closes when no candidate is
  left undecided.

One open ask per ``(chain, SYMBOL)`` (or ``(chain, address)`` when there is no
symbol) at a time: a repeated refusal refreshes it and never re-notifies.

⚠️ The decider is the ONLY writer and it runs from an owner seat. There is no
agent path to it: ``owner_ask(answer=)`` and ``/fulfill`` skip this ask kind
(``core.goal_vocab.has_own_surface``).
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from core.goal_vocab import ASK_KIND_TOKEN_IDENTITY as ASK_KIND

logger = logging.getLogger(__name__)

#: Fire-and-forget owner notices spawned from the (sync) gate.
_NOTICES: set = set()


def _board(container: Any = None):
    from agents.task.goals.board import GoalBoard
    from core.runtime_paths import container_data_home, goals_db_path
    home = container_data_home(container) if container is not None else None
    return GoalBoard(goals_db_path(home))


def dedup_key(chain: str, symbol: str, address: str) -> str:
    sym = str(symbol or "").strip().upper()
    return f"{chain}:{sym}" if sym else f"{chain}:{str(address or '').lower()}"


def _ts_words(ts: Optional[float]) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(float(ts)))
    except Exception:
        return ""


def describe_candidate(user_id: str, chain: str, address: str, *,
                       symbol: str = "", name: str = "", role: str = "") -> Dict[str, Any]:
    """What the owner needs to tell two same-symbol contracts apart: its name,
    what we know about where it came from, and when it was first seen."""
    from core.wallet.token_trust import SOURCE_WORDS
    rec: Dict[str, Any] = {}
    try:
        from core.wallet.tokens import frozen_record
        rec = frozen_record(chain, address) or {}
    except Exception:
        rec = {}
    facts: List[str] = []
    try:
        from core.wallet.token_provenance import own_token
        if own_token(chain, address) is not None:
            facts.append(SOURCE_WORDS["own_launch"])
    except Exception:
        pass
    try:
        from core import open_positions
        pos = open_positions.get_position(user_id, chain, address)
    except Exception:
        pos = None
    if pos is not None:
        cost = float(pos.entry_usd or 0.0)
        facts.append(f"I hold {pos.qty:,.6g}"
                     + (f", bought for ${cost:,.2f}" if cost > 0 else "")
                     + (f" since {_ts_words(pos.entry_ts)}" if pos.entry_ts else ""))
    if not facts:
        facts.append("no launch record, no owner decision, not held")
    return {
        "address": address,
        "symbol": str(symbol or rec.get("symbol") or ""),
        "name": str(name or rec.get("name") or ""),
        "provenance": "; ".join(facts),
        "first_seen_ts": rec.get("first_seen_ts"),
        "role": role,
        "decision": None,
    }


def _find_open(board: Any, user_id: str, key: str):
    from core.goal_vocab import ASK_OPEN
    for ask in board.asks(user_id=user_id, status=ASK_OPEN):
        p = ask.payload or {}
        if p.get("ask_kind") == ASK_KIND and p.get("dedup_key") == key:
            return ask
    return None


def raise_identity_ask(*, user_id: str, chain: str, symbol: str,
                       candidates: List[Dict[str, Any]], reason: str,
                       container: Any = None, board: Any = None
                       ) -> Optional[Tuple[str, bool]]:
    """Raise (or refresh) the ask. ``(ask_id, created)``, or None on failure.

    Fail-open: a store fault returns None and the refusal still stands."""
    from core.wallet.addresses import same_address
    if not user_id or not candidates:
        return None
    try:
        board = board or _board(container)
        key = dedup_key(chain, symbol, candidates[0]["address"])
        ask = _find_open(board, user_id, key)
        if ask is not None:
            have = list((ask.payload or {}).get("candidates") or [])
            added = [c for c in candidates
                     if not any(same_address(c["address"], h.get("address", "")) for h in have)]
            if added:
                board.merge_payload(ask.id, {"candidates": have + added})
            return str(ask.id), False
        sym = str(symbol or "").strip().upper()
        what = (f"Which {sym} is the real token on {chain}?" if sym and len(candidates) > 1
                else f"Trust {sym or 'token'} {candidates[0]['address']} on {chain}?")
        ask = board.create_ask(
            user_id=user_id, what=what, why=str(reason or "")[:1200],
            extra_payload={"ask_kind": ASK_KIND, "dedup_key": key, "chain": chain,
                           "symbol": sym, "reason": str(reason or "")[:600],
                           "candidates": list(candidates)},
            force=True)
    except Exception:
        logger.warning("token identity ask could not be raised", exc_info=True)
        return None
    _notify_soon(container, user_id, str(ask.id), render_notice(ask))
    return str(ask.id), True


def _item(ask: Any, idx: int) -> Dict[str, Any]:
    return {"kind": ASK_KIND, "id": f"{ask.id}.{idx}"}


def render_notice(ask: Any) -> str:
    """The owner notice: every candidate with its own two taps."""
    from core.self_evolution import pending_tap_token
    p = ask.payload or {}
    lines = [f"🪙 {ask.title}", f"I refused a buy: {p.get('reason') or ask.body}"[:500],
             "Nothing was bought."]
    for i, c in enumerate(p.get("candidates") or []):
        if c.get("decision"):
            continue
        seen = _ts_words(c.get("first_seen_ts"))
        lines.append(f"• {c.get('symbol') or '?'} {c['address']}"
                     + (f" “{c['name']}”" if c.get("name") else "")
                     + f" — {c.get('provenance')}"
                     + (f"; first seen {seen}" if seen else ""))
        item = _item(ask, i)
        lines.append(f"   trust it: {pending_tap_token('approve', item)}   "
                     f"not trusted: {pending_tap_token('reject', item)}")
    lines.append("Or decide later in /pending.")
    return "\n".join(lines)


def _notify_soon(container: Any, user_id: str, ask_id: str, text: str) -> None:
    """One owner notice, fire-and-forget from the gate's sync frame. Without a
    running loop the ask still waits in /pending — it is the durable record."""
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        from core.async_bridge import spawn_retained
        from core.surfaces.user_delivery import deliver_user_message
        spawn_retained(deliver_user_message(container, user_id, text,
                                            source="token_identity", ask_id=ask_id),
                       _NOTICES)
    except Exception:
        logger.debug("token identity ask: owner notice skipped", exc_info=True)


def list_pending_items(board: Any, user_id: str) -> List[Dict[str, Any]]:
    """One pending item per UNDECIDED candidate of every open ask, shaped like
    ``approval_queue.list_pending_tool_approvals`` items."""
    from core.goal_vocab import ASK_OPEN
    out: List[Dict[str, Any]] = []
    for ask in board.asks(user_id=user_id, status=ASK_OPEN):
        p = ask.payload or {}
        if p.get("ask_kind") != ASK_KIND:
            continue
        cands = p.get("candidates") or []
        for i, c in enumerate(cands):
            if c.get("decision"):
                continue
            seen = _ts_words(c.get("first_seen_ts"))
            others = sum(1 for j, o in enumerate(cands) if j != i and not o.get("decision"))
            preview = (f"{c.get('symbol') or p.get('symbol') or '?'} on {p.get('chain')}: "
                       f"trust {c['address']}"
                       + (f" “{c['name']}”" if c.get("name") else "")
                       + f"? ({c.get('provenance')}"
                       + (f"; first seen {seen}" if seen else "") + ")"
                       + (f" — {others} other contract(s) claim it" if others else "")
                       + ". Approve = trust it; reject = not trusted, quarantined.")
            out.append({"kind": ASK_KIND, "id": f"{ask.id}.{i}", "chars": len(preview),
                        "preview": preview, "tool": None})
    return out


def _split(item_id: str) -> Tuple[str, Optional[int]]:
    head, _, tail = str(item_id or "").rpartition(".")
    try:
        return head, int(tail)
    except ValueError:
        return str(item_id or ""), None


def decide_item(board: Any, item_id: str, *, approve: bool, user_id: str,
                pins_db: Optional[str] = None,
                positions_db: Optional[str] = None) -> Tuple[bool, str]:
    """The owner's tap on one candidate. ``(ok, message)``."""
    from core.goal_vocab import ASK_OPEN
    from core.wallet import token_trust
    ask_id, idx = _split(item_id)
    ask = board.get(ask_id, user_id=user_id) if ask_id else None
    p = (ask.payload or {}) if ask is not None else {}
    if (ask is None or ask.status != ASK_OPEN or p.get("ask_kind") != ASK_KIND
            or idx is None or not (0 <= idx < len(p.get("candidates") or []))):
        return False, f"no open token-identity question '{item_id}' — see /pending"
    cands = [dict(c) for c in p.get("candidates") or []]
    cand = cands[idx]
    if cand.get("decision"):
        return False, f"{cand['address']} was already decided ({cand['decision']})"
    chain = str(p.get("chain") or "")
    ctx = token_trust.owner_seat_ctx(user_id)
    symbol = cand.get("symbol") or p.get("symbol") or None
    if approve:
        ok, msg = token_trust.trust(ctx, chain, cand["address"], symbol,
                                    note=f"approved on ask {ask_id[:12]}",
                                    pins_db=pins_db, positions_db=positions_db)
        if not ok:
            return False, msg
        cand["decision"] = "trusted"
        moved = []
        for j, other in enumerate(cands):
            if j == idx or other.get("decision"):
                continue
            if token_trust._set_status(
                    user_id, chain, other["address"], "quarantined",
                    f"look-alike: the owner chose {cand['address']} as {symbol or 'the token'}",
                    positions_db):
                moved.append(other["address"])
        board.merge_payload(ask.id, {"candidates": cands})
        board.decide_ask(ask.id, user_id=user_id, approved=True,
                         answer=f"trust {cand['address']}", answer_via="owner seat")
        if moved:
            msg += " Quarantined the look-alike position(s): " + ", ".join(moved) + "."
        return True, msg
    ok, msg = token_trust.untrust(ctx, chain, cand["address"], symbol=symbol or "",
                                  note=f"rejected on ask {ask_id[:12]}",
                                  pins_db=pins_db, positions_db=positions_db)
    if not ok:
        return False, msg
    cand["decision"] = "rejected"
    board.merge_payload(ask.id, {"candidates": cands})
    if all(c.get("decision") for c in cands):
        board.decide_ask(ask.id, user_id=user_id, approved=False,
                         answer="no candidate trusted", answer_via="owner seat")
    return True, msg


def open_ask_naming(board: Any, user_id: str, text: str):
    """The open token-identity ask whose candidate address appears in *text*,
    or None — so the agent's ``owner_ask`` does not ask the same thing twice."""
    from core.goal_vocab import ASK_OPEN
    low = str(text or "").lower()
    if not low:
        return None
    for ask in board.asks(user_id=user_id, status=ASK_OPEN):
        p = ask.payload or {}
        if p.get("ask_kind") != ASK_KIND:
            continue
        for c in p.get("candidates") or []:
            addr = str(c.get("address") or "").lower()
            if addr and (addr in low or addr[:10] in low):
                return ask
        sym = str(p.get("symbol") or "").lower()
        if (len(sym) >= 3 and re.search(rf"(?<![a-z0-9]){re.escape(sym)}(?![a-z0-9])", low)
                and any(w in low for w in ("contract", "address", "real", "which"))):
            return ask
    return None
