"""The owner's token-trust decisions, from any owner seat (W1).

Before W1 the only way to say "this contract is the real PNL" was a server
CLI (``polyrob wallet pin-token``), and a look-alike the wallet held blocked the
real token until somebody opened a shell. The owner runs the agent from a phone.
This module is the ONE implementation every owner seat calls:

* ``trust``        — write an ``owner_approved`` binding into the pin store
                     (``core.wallet.token_pins``), keyed on ``(chain, address)``;
* ``untrust``      — the owner's NOT-trusted verdict: remove any pin, record a
                     rejection, and quarantine a held position at that address;
* ``write_off``    — a holding's loss verdict: ``written_off``, realized loss =
                     the cost basis the rail recorded;
* ``unquarantine`` — undo a quarantine (the identity gate's automatic one, or
                     the owner's own ``untrust``);
* ``trust_view``   — which tokens are trusted and why, plus the rejected
                     contracts and every quarantined / written-off holding.

⚠️ No agent tool calls this. Every writer takes an execution context and
refuses unless it is a genuine owner turn (``core.security.owner_turn``): the
seats pass :func:`owner_seat_ctx`. A trust the agent could write is a
verification the agent could grant itself.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: How each trust source reads to the owner.
SOURCE_WORDS = {
    "canonical": "canonical (the chain registry)",
    "own_launch": "our own launch",
    "owner_pin": "owner pin (CLI)",
    "owner_approved": "owner approved",
}


def owner_seat_ctx(user_id: str) -> SimpleNamespace:
    """The execution context of an owner seat verb (Telegram, the REPL, the
    console, the CLI): the owner at the top of a turn, never a re-entry."""
    return SimpleNamespace(user_id=str(user_id or ""), role="orchestrator",
                           is_sub_agent=False, metadata={"turn_kind": "owner_seat"})


def _refusal(ctx: Any, verb: str) -> Optional[str]:
    from core.security.owner_turn import owner_turn_refusal
    return owner_turn_refusal(ctx, verb=verb, does="decides which tokens are trusted",
                              public="decide which tokens are trusted")


def short(address: str) -> str:
    a = str(address or "")
    return a if len(a) <= 14 else f"{a[:8]}…{a[-4:]}"


def _norm_chain(chain: str) -> str:
    from core.wallet.token_pins import norm_chain
    return norm_chain(chain)


def _unknown_chain(chain: str) -> Optional[str]:
    """Why *chain* (already normalized) is not a chain the gates key on, or None.
    A pin under a name no gate reads ('eth' for 'ethereum') is a trust the owner
    was told he gave and the identity check never sees."""
    try:
        from core.wallet import chains
        if chains.get(chain) is not None:
            return None
        known = ", ".join(sorted(chains.names()))
    except Exception:
        return None
    return f"{chain!r} is not a chain name I use (one of: {known}). Nothing was written."


def _position(user_id: str, chain: str, address: str, db_path: Optional[str]):
    from core import open_positions
    try:
        return open_positions.get_position(user_id, chain, address, db_path=db_path)
    except Exception:
        logger.debug("token trust: position read failed", exc_info=True)
        return None


def _symbol_for(user_id: str, chain: str, address: str,
                positions_db: Optional[str]) -> str:
    pos = _position(user_id, chain, address, positions_db)
    if pos is not None and pos.symbol:
        return str(pos.symbol)
    try:
        from core.wallet.tokens import frozen_record
        rec = frozen_record(chain, address)
        if rec and rec.get("symbol"):
            return str(rec["symbol"])
    except Exception:
        pass
    return ""


def trust(ctx: Any, chain: str, address: str, symbol: Optional[str] = None, *,
          note: str = "", pins_db: Optional[str] = None,
          positions_db: Optional[str] = None) -> Tuple[bool, str]:
    """Trust ``(chain, address)`` on the owner's word (``owner_approved``)."""
    refusal = _refusal(ctx, "trust token")
    if refusal:
        return False, refusal
    chain = _norm_chain(chain)
    bad_chain = _unknown_chain(chain)
    if bad_chain:
        return False, f"Not trusted: {bad_chain}"
    uid = str(getattr(ctx, "user_id", "") or "")
    sym = (symbol or "").strip() or _symbol_for(uid, chain, address, positions_db)
    if not sym:
        return False, (f"I do not know the symbol of {address} on {chain}. "
                       f"Name it: /wallet trust {chain} {address} <SYMBOL> go")
    from core.wallet import token_pins
    try:
        row = token_pins.pin(chain, address, sym, note=note or "owner approved",
                             source=token_pins.SOURCE_OWNER_APPROVED, db_path=pins_db)
    except ValueError as exc:
        return False, f"Not trusted: {exc}"
    except Exception as exc:
        logger.warning("token trust write failed", exc_info=True)
        return False, f"Not trusted: the token store could not be written ({exc})."
    msg = (f"✅ Trusted {row['symbol']} on {chain}: {row['address']} "
           f"(owner approved). Buys of it pass the identity check.")
    if row.get("replaced"):
        msg += f" It replaces the earlier {row['symbol']} binding: {row['replaced']}."
    _emit(_kinds().TOKEN_TRUSTED, uid, chain, row["address"], row["symbol"])
    return True, msg


def untrust(ctx: Any, chain: str, address: str, *, note: str = "",
            symbol: Optional[str] = None, pins_db: Optional[str] = None,
            positions_db: Optional[str] = None) -> Tuple[bool, str]:
    """The owner says ``(chain, address)`` is NOT the token: drop its pin,
    record the rejection, quarantine a held position at that address."""
    refusal = _refusal(ctx, "untrust token")
    if refusal:
        return False, refusal
    chain = _norm_chain(chain)
    bad_chain = _unknown_chain(chain)
    if bad_chain:
        return False, f"Not recorded: {bad_chain}"
    uid = str(getattr(ctx, "user_id", "") or "")
    sym = (symbol or "").strip() or _symbol_for(uid, chain, address, positions_db)
    from core.wallet import token_pins
    try:
        row = token_pins.reject(chain, address, symbol=sym,
                                note=note or "owner: not trusted", db_path=pins_db)
    except ValueError as exc:
        return False, f"Not recorded: {exc}"
    except Exception as exc:
        logger.warning("token untrust write failed", exc_info=True)
        return False, f"Not recorded: the token store could not be written ({exc})."
    quarantined = _set_status(uid, chain, address, "quarantined",
                              f"owner: not trusted{(' — ' + note) if note else ''}",
                              positions_db)
    _emit(_kinds().TOKEN_UNTRUSTED, uid, chain, row["address"], sym)
    msg = (f"🚫 {sym or 'Token'} {row['address']} on {chain} is marked NOT trusted. "
           f"I will not buy it and will not ask about it again.")
    if quarantined:
        msg += (" The position I hold at that address is quarantined — it keeps "
                "its cost, and it no longer claims the symbol.")
    return True, msg


def _set_status(uid: str, chain: str, address: str, status: str, reason: str,
                positions_db: Optional[str]) -> bool:
    from core import open_positions
    try:
        return open_positions.set_status(uid, chain, address, status, reason=reason,
                                         db_path=positions_db)
    except Exception:
        logger.warning("token trust: position status write failed", exc_info=True)
        return False


def write_off(ctx: Any, chain: str, address: str, *, reason: str = "",
              execute: bool = False,
              positions_db: Optional[str] = None) -> Tuple[bool, str]:
    """Write a holding off: status ``written_off``, realized loss = cost basis.

    ``execute=False`` is the confirm step: it says what WOULD happen and changes
    nothing."""
    refusal = _refusal(ctx, "writeoff")
    if refusal:
        return False, refusal
    chain = _norm_chain(chain)
    uid = str(getattr(ctx, "user_id", "") or "")
    pos = _position(uid, chain, address, positions_db)
    if pos is None:
        return False, (f"I track no position at {address} on {chain}. /book lists "
                       f"what the rail tracks.")
    if pos.status == "written_off":
        return False, f"{pos.symbol or address} on {chain} is already written off."
    loss = pos.entry_usd
    loss_txt = (f"${float(loss):,.2f} (its recorded cost)" if loss is not None
                else "basis unknown — no trade recorded what it cost, so the loss "
                     "is not a known figure")
    what = (f"{pos.symbol or '?'} {pos.address} on {chain} — {pos.qty:,.6g} held, "
            f"now {pos.status}")
    if not execute:
        return True, (f"Write off {what}?\nRealized loss: {loss_txt}. The tokens stay "
                      f"in the wallet; nothing is sold or sent. Confirm:\n"
                      f"/writeoff {chain} {pos.address} go"
                      + (f" {reason}" if reason else ""))
    why = (reason or "owner write-off").strip()
    ok = _set_status(uid, chain, pos.address, "written_off",
                     f"{why}; realized loss "
                     + (f"${float(loss):,.2f} (cost basis)" if loss is not None
                        else "unknown (basis unknown)"), positions_db)
    if not ok:
        return False, f"The write-off of {what} was not recorded (the store refused)."
    _emit(_kinds().POSITION_WRITTEN_OFF, uid, chain, pos.address, pos.symbol or "",
          loss_usd=(None if loss is None else round(float(loss), 2)))
    return True, (f"✅ Written off {what.rsplit(', now', 1)[0]}. Realized loss "
                  f"{loss_txt}. Reason: {why}.")


def unquarantine(ctx: Any, chain: str, address: str, *, execute: bool = False,
                 pins_db: Optional[str] = None,
                 positions_db: Optional[str] = None) -> Tuple[bool, str]:
    """Undo a quarantine: the position is ``open`` again and the owner's
    NOT-trusted verdict on that address (if any) is lifted."""
    refusal = _refusal(ctx, "unquarantine")
    if refusal:
        return False, refusal
    chain = _norm_chain(chain)
    uid = str(getattr(ctx, "user_id", "") or "")
    pos = _position(uid, chain, address, positions_db)
    if pos is None or pos.status != "quarantined":
        return False, (f"No quarantined position at {address} on {chain}. /book "
                       f"lists what is quarantined.")
    if not execute:
        return True, (f"Undo the quarantine of {pos.symbol or '?'} {pos.address} on "
                      f"{chain} ({pos.status_reason or 'no reason recorded'})? It "
                      f"claims its symbol again, so a buy of another contract with "
                      f"that symbol is refused until one of them is trusted. Confirm:\n"
                      f"/unquarantine {chain} {pos.address} go")
    ok = _set_status(uid, chain, pos.address, "open", "owner: quarantine undone",
                     positions_db)
    try:
        from core.wallet import token_pins
        token_pins.unreject(chain, pos.address, db_path=pins_db)
    except Exception:
        logger.debug("token trust: unreject skipped", exc_info=True)
    if not ok:
        return False, "The quarantine was not lifted (the store refused)."
    return True, f"✅ {pos.symbol or '?'} {pos.address} on {chain} is open again."


def _kinds():
    from core import event_kinds
    return event_kinds


def _emit(kind: str, uid: str, chain: str, address: str, symbol: str, **extra) -> None:
    try:
        from core.event_log import emit
        emit(kind, source="token_trust", user_id=uid,
             attrs={"chain": chain, "address": address, "symbol": symbol, **extra})
    except Exception:
        logger.debug("token trust: event not recorded", exc_info=True)


# --------------------------------------------------------------------------- #
# the read
# --------------------------------------------------------------------------- #

def trust_view(user_id: str, *, pins_db: Optional[str] = None,
               positions_db: Optional[str] = None,
               provenance_db: Optional[str] = None) -> Dict[str, Any]:
    """Which tokens are trusted and why, what the owner rejected, and every
    quarantined / written-off holding. Each source is read on its own; one that
    cannot be read is NAMED in ``unreadable``, never shown as empty."""
    out: Dict[str, Any] = {"trusted": [], "rejected": [], "holdings": [],
                           "unreadable": []}
    try:
        from core.wallet.tokens import CANONICAL_TOKENS
        for (chain, addr), meta in sorted(CANONICAL_TOKENS.items()):
            out["trusted"].append({"chain": chain, "address": addr,
                                   "symbol": str(meta.get("symbol") or ""),
                                   "source": "canonical", "note": ""})
    except Exception:
        out["unreadable"].append("the canonical list")
    try:
        from core.wallet.token_provenance import all_own_tokens
        for row in all_own_tokens(db_path=provenance_db):
            sym = ""
            try:
                from core.wallet.tokens import frozen_record
                sym = str((frozen_record(row["chain"], row["address"]) or {})
                          .get("symbol") or "")
            except Exception:
                pass
            out["trusted"].append({"chain": row["chain"], "address": row["address"],
                                   "symbol": sym, "source": "own_launch",
                                   "note": row.get("kind") or ""})
    except Exception:
        out["unreadable"].append("our own launches")
    from core.wallet import token_pins
    state, pins, _err = token_pins.pins_status(db_path=pins_db)
    if state == "unreadable":
        out["unreadable"].append("the owner token pins")
    for row in pins:
        out["trusted"].append({"chain": row["chain"], "address": row["address"],
                               "symbol": row["symbol"],
                               "source": row.get("source") or "owner_pin",
                               "note": row.get("note") or ""})
    if state != "unreadable":
        try:
            out["rejected"] = token_pins.all_rejections(db_path=pins_db, strict=True)
        except token_pins.PinStoreUnreadable:
            out["unreadable"].append("the owner's not-trusted list")
    try:
        from core import open_positions
        entries = open_positions.entries_for(user_id, db_path=positions_db, strict=True)
        for entry in entries.values():
            if entry.status == "open":
                continue
            out["holdings"].append({
                "chain": entry.chain, "address": entry.address,
                "symbol": entry.symbol or "", "qty": entry.qty,
                "entry_usd": entry.entry_usd, "status": entry.status,
                "reason": entry.status_reason})
    except Exception:
        out["unreadable"].append("the tracked positions")
    return out


def render_trust_view(view: Dict[str, Any], *, now: Optional[float] = None) -> str:
    """The text the Telegram and REPL seats show for ``/wallet tokens``."""
    lines = ["Tokens I trust (a buy of any other contract is checked first):"]
    for t in view.get("trusted") or []:
        lines.append(f"• {t.get('symbol') or '?'} on {t['chain']}: {t['address']} — "
                     f"{SOURCE_WORDS.get(t['source'], t['source'])}")
    rejected = view.get("rejected") or []
    if rejected:
        lines += ["", "You marked NOT trusted (never bought, never asked again):"]
        for r in rejected:
            lines.append(f"• {r.get('symbol') or '?'} on {r['chain']}: {r['address']}"
                         + (f" — {r['note']}" if r.get("note") else ""))
    holdings = view.get("holdings") or []
    if holdings:
        lines += ["", "Holdings out of the book's open positions:"]
        for h in holdings:
            cost = h.get("entry_usd")
            word = "quarantined" if h["status"] == "quarantined" else "written off"
            lines.append(f"• {h.get('symbol') or '?'} on {h['chain']}: {h['address']} — "
                         f"{word}, "
                         + (f"cost ${float(cost):,.2f}" if cost is not None
                            else "basis unknown")
                         + (f" ({h['reason']})" if h.get("reason") else ""))
            if h["status"] == "quarantined":
                lines.append(f"   undo: /unquarantine {h['chain']} {h['address']}   "
                             f"write off: /writeoff {h['chain']} {h['address']}")
    for what in view.get("unreadable") or []:
        lines.append(f"⚠ I could not read {what} — this list may be incomplete.")
    lines += ["", "Trust one: /wallet trust <chain> <address> [SYMBOL] · "
                  "untrust: /wallet untrust <chain> <address>"]
    return "\n".join(lines)


def tokens_reply(user_id: Optional[str], args: List[str], *,
                 pins_db: Optional[str] = None,
                 positions_db: Optional[str] = None) -> str:
    """``/wallet tokens | trust | untrust`` — the shared text seat (Telegram +
    REPL). A write needs ``go`` as its last word; without it the verb says what
    it would do and changes nothing."""
    if not user_id:
        return "Only the owner decides which tokens are trusted."
    sub = (args[0].lower() if args else "tokens")
    rest = [a for a in args[1:] if str(a).strip()]
    if sub in ("tokens", "token"):
        return render_trust_view(trust_view(user_id, pins_db=pins_db,
                                            positions_db=positions_db))
    go = bool(rest) and rest[-1].lower() in ("go", "confirm")
    if go:
        rest = rest[:-1]
    if len(rest) < 2:
        return (f"Usage: /wallet {sub} <chain> <address>"
                + (" [SYMBOL]" if sub == "trust" else "") + " [go]")
    chain, address = rest[0], rest[1]
    ctx = owner_seat_ctx(user_id)
    if sub == "trust":
        symbol = rest[2] if len(rest) > 2 else None
        if not go:
            return (f"Trust {address} on {chain.lower()}"
                    + (f" as {symbol.upper()}" if symbol else "")
                    + "? Every autonomous buy of it then passes the identity check, "
                      "and a held look-alike of its symbol is quarantined. Confirm:\n"
                      f"/wallet trust {chain.lower()} {address}"
                    + (f" {symbol}" if symbol else "") + " go")
        return trust(ctx, chain, address, symbol, pins_db=pins_db,
                     positions_db=positions_db)[1]
    if sub == "untrust":
        if not go:
            return (f"Mark {address} on {chain.lower()} NOT trusted? I will never buy "
                    f"it, and a position I hold there is quarantined. Confirm:\n"
                    f"/wallet untrust {chain.lower()} {address} go")
        return untrust(ctx, chain, address, pins_db=pins_db,
                       positions_db=positions_db)[1]
    return "Usage: /wallet tokens | trust <chain> <address> [SYMBOL] [go] | untrust <chain> <address> [go]"


def _split_go(args: List[str]) -> Tuple[List[str], bool, str]:
    """``<chain> <address> [go] [reason…]`` -> (positional, go, reason)."""
    words = [str(a) for a in (args or []) if str(a).strip()]
    head, tail = words[:2], words[2:]
    go = bool(tail) and tail[0].lower() in ("go", "confirm")
    if go:
        tail = tail[1:]
    return head, go, " ".join(tail).strip()


def writeoff_reply(user_id: Optional[str], args: List[str], *,
                   positions_db: Optional[str] = None) -> str:
    """``/writeoff <chain> <address> [go] [reason…]``."""
    if not user_id:
        return "Only the owner writes a holding off."
    head, go, reason = _split_go(args)
    if len(head) < 2:
        return ("Usage: /writeoff <chain> <address> [go] [reason]\n"
                "Bare = what it would do; `go` records it. /book lists what I track.")
    return write_off(owner_seat_ctx(user_id), head[0], head[1], reason=reason,
                     execute=go, positions_db=positions_db)[1]


def unquarantine_reply(user_id: Optional[str], args: List[str], *,
                       pins_db: Optional[str] = None,
                       positions_db: Optional[str] = None) -> str:
    """``/unquarantine <chain> <address> [go]``."""
    if not user_id:
        return "Only the owner lifts a quarantine."
    head, go, _reason = _split_go(args)
    if len(head) < 2:
        return ("Usage: /unquarantine <chain> <address> [go]\n"
                "Bare = what it would do; `go` lifts it. /wallet tokens lists them.")
    return unquarantine(owner_seat_ctx(user_id), head[0], head[1], execute=go,
                        pins_db=pins_db, positions_db=positions_db)[1]


__all__ = ["SOURCE_WORDS", "owner_seat_ctx", "trust", "untrust", "write_off",
           "unquarantine", "trust_view", "render_trust_view", "tokens_reply",
           "writeoff_reply", "unquarantine_reply", "short"]

