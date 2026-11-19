"""The agent's own money state beside its position book (120 W4).

Two reads ``defi_data.positions`` prints after the book, and the one bridge
render ``polyrob wallet bridges`` prints too:

* the trading mandate — ``core.trading_report.trading_view`` with no price read
  (the ``/trade status`` text); the agent has no breaker release;
* the bridges in flight — ``core.wallet.bridge_guard.open_bridges``, rendered by
  :func:`open_bridge_lines`. An unreadable store is UNKNOWN, never "none".

The chain-name rule lives here and not in ``core``: a bridge row stores the
PROVIDER's own pseudo id for a Solana origin, a ``tools``-tier constant that
``core`` may not import (``tests/test_layering_ratchet.py``).
"""
import datetime as _dt
from typing import List, Optional


def bridge_chain_key(chain_id) -> Optional[str]:
    """The registry key for a chain id ON A BRIDGE ROW, or ``None``.

    ``bridge_guard.chain_name_for_id`` owns the registry ids (and the rule that
    a falsey id is NOT Solana). The other id a row may carry is the bridge
    provider's pseudo id for Solana, resolved from the module that owns it.
    """
    from core.wallet.bridge_guard import chain_name_for_id
    name = chain_name_for_id(chain_id)
    if name:
        return name
    try:
        from tools.defi.providers.relay_bridge import SOLANA_CHAIN_ID
        if int(chain_id) == int(SOLANA_CHAIN_ID):
            return "solana"
    except Exception:
        pass
    return None


def chain_label(chain_id) -> str:
    """043 D1: the chain NAME with its id beside it, or ``chain <id>``."""
    name = bridge_chain_key(chain_id)
    if not name:
        return f"chain {chain_id}"
    return f"{name} (chain {chain_id})"


def tx_link(chain_id, tx_ref) -> str:
    """An explorer link for a transaction that LANDED on *chain_id*, or ``""``.

    ⚠️ ``tx_ref`` on a bridge row is the ORIGIN transaction, so it is resolved
    against the ORIGIN chain, and there is no link when that chain cannot be
    named (``core.wallet.tx_notify``'s rule).
    """
    if not tx_ref:
        return ""
    from core.wallet.chains import explorer_url
    name = bridge_chain_key(chain_id)
    if not name:
        return ""
    return explorer_url(name, "tx", str(tx_ref)) or ""


def open_bridge_lines(rows) -> List[str]:
    """The bridge rows that have not been proven to arrive, as text lines."""
    if not rows:
        return ["No bridges awaiting confirmation."]
    out = [f"{len(rows)} bridge(s) awaiting confirmation:"]
    for r in rows:
        # C61: one row missing a stamp must not take the listing down.
        try:
            when = _dt.datetime.fromtimestamp(
                float(r.get("created_at") or 0),
                tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ")
        except (TypeError, ValueError, OverflowError, OSError):
            when = "time unknown"
        usd = "" if r.get("amount_usd") is None else f" ${float(r['amount_usd']):,.2f}"
        out.append(f"  {r.get('id') or '(no id)'}  [{r.get('state') or '?'}]{usd}"
                   f"  -> {chain_label(r.get('dest_chain_id'))}  {when}")
        out.append(f"     relay request: {r.get('request_id') or 'unknown'}")
        link = tx_link(r.get("origin_chain_id"), r.get("tx_ref"))
        if link:
            out.append(f"     {link}")
        elif r.get("tx_ref"):
            out.append(f"     tx {r['tx_ref']}")
        if r.get("detail"):
            out.append(f"     {str(r['detail'])[:300]}")
    return out


def own_state_lines(user_id: str, execution_context=None) -> List[str]:
    """The mandate block and the bridges block for ``defi_data.positions``.
    Never raises: each unreadable store is one named line."""
    from core.money.authority import money_read_refusal
    if money_read_refusal(execution_context):
        return ["Trading mandate and bridges: not shown (the owner's read; never a "
                "delegated sub-agent's)."]
    return ["Trading mandate:"] + _mandate_lines(user_id) + \
        ["Bridges in flight:"] + _bridge_lines(user_id)


def _mandate_lines(user_id: str) -> List[str]:
    try:
        from core.trading_report import trading_view
        view = trading_view(user_id)
    except Exception as exc:
        return [f"  UNKNOWN — the trading report could not be built ({type(exc).__name__})"]
    lines = [f"  {ln}" for ln in view.get("lines") or []]
    if view.get("halted"):
        lines.append("  Only the owner releases a breaker (/trade release); you have no "
                     "release. Say so; do not work around it.")
    return lines


def _bridge_lines(user_id: str) -> List[str]:
    try:
        from core.wallet import bridge_guard
        rows = bridge_guard.open_bridges(user_id)
    except Exception as exc:
        return [f"  UNKNOWN — the bridge store could not be read ({type(exc).__name__}); "
                f"this is not 'no bridges in flight'."]
    lines = [f"  {ln}" for ln in open_bridge_lines(rows)]
    if rows:
        lines.append("  An in_flight bridge is money in motion: never send it again (a "
                     "second send pays twice); the watcher settles it.")
    return lines


__all__ = ["bridge_chain_key", "chain_label", "open_bridge_lines", "own_state_lines",
           "tx_link"]
