"""The income the rule reads — from the EXISTING records only (proposal 122 W4).

No second ledger: each reader turns one record the rail already keeps into
``core.wallet.income_rule.IncomeEvent`` rows (a stable key, the kind, when, the USD value or
None when it cannot be valued now). The rule counts each key once.

| Kind | The record | Its value |
|---|---|---|
| ``fees`` | the spend audit's ``launchpad_claim`` rows (``income_raw``: the owed balance the guard asserted); a row whose receipt is still pending waits, a reverted or unknown one never counts | the native wei at the native price now |
| ``nft`` | the holdings watch's ``lost`` rows with the pinned market's ``Sold`` facts (``core/wallet/holdings_state.py``) | price less royalty, native or a canonical token |
| ``payments`` | ``x402_payment_requests`` rows ``completed`` on an income network (offers, x402 and invoices are one store) | ``amount_usd`` |
| ``trading`` | the position book's realized totals, the treasury AND every NFT account (``core.open_positions.realized_for(all_accounts=True)``) | the growth above the rule's mark |

A reader that fails names the failure (``errors``) and adds nothing: an unreadable record is
not "no income", and its events are read again on the next tick.
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.wallet.income_rule import IncomeEvent

logger = logging.getLogger(__name__)

ZERO = "0x" + "0" * 40


def _price(chain: str, token: Optional[str], price=None) -> Optional[float]:
    """The USD price of *token* on *chain* (``None`` = the native coin), as the guard reads it.
    *price* ``(chain, token_or_None)`` replaces the reader (tests)."""
    if price is not None:
        return price(chain, token)
    from tools.defi.price_sources import native_spend_price, spend_price
    return native_spend_price(chain) if token is None else spend_price(chain, token)


def _native_decimals(chain: str) -> int:
    from core.wallet import chains
    row = chains.get(chain)
    return int(getattr(row, "native_decimals", 18) or 18)


def _value_native(chain: str, raw: int, price=None) -> Optional[float]:
    unit = _price(chain, None, price)
    if unit is None:
        return None
    return int(raw) / 10 ** _native_decimals(chain) * float(unit)


# ---------------------------------------------------------------------------- fees

def fee_claims(*, audit_path: Optional[str] = None, price=None) -> List[IncomeEvent]:
    """Launch fee claims from the spend audit. Raises when the audit cannot be read."""
    from core.money.receipt_state import receipt_states
    from core.wallet.trade_index import audit_path as _default
    path = audit_path or _default()
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue            # one corrupt line never hides the rest
            if isinstance(e, dict):
                entries.append(e)
    states = receipt_states(entries)
    out = []
    for e in entries:
        if e.get("action") != "launchpad_claim" or not e.get("income_raw") or not e.get("result_ref"):
            continue
        ref, chain = str(e["result_ref"]), str(e.get("chain") or "")
        state = states.get(ref)
        if state is not None and state != "confirmed":
            continue                # pending: read again later; reverted/unknown: nothing came
        raw = int(e["income_raw"])
        holder = f" in account {e['account']}" if e.get("account") else ""
        out.append(IncomeEvent(
            key=f"fees:{ref.lower()}", kind="fees", at=float(e.get("ts") or 0),
            usd=_value_native(chain, raw, price),
            detail=f"fee claim {raw / 10 ** _native_decimals(chain):.6g} native on {chain}"
                   f"{holder}, tx {ref}"))
    return out


# ---------------------------------------------------------------------------- NFT sales

def _value_currency(chain: str, currency: str, raw: int, price=None) -> Optional[float]:
    if not currency or currency.lower() == ZERO:
        return _value_native(chain, raw, price)
    from core.wallet.tokens import CANONICAL_TOKENS
    hit = next((meta for (c, a), meta in CANONICAL_TOKENS.items()
                if c == chain and a.lower() == currency.lower()), None)
    if hit is None:
        return None                 # a currency with no verified identity is not valued
    unit = _price(chain, currency, price)
    if unit is None:
        return None
    return int(raw) / 10 ** int(hit.get("decimals") or 18) * float(unit)


def nft_sales(*, state: Optional[Dict[str, Any]] = None, price=None) -> List[IncomeEvent]:
    """NFT sales from the holdings watch's lost rows that carry a ``Sold`` event. Raises when
    the holdings state cannot be read."""
    if state is None:
        from core.instance import resolve_owner_user_id
        from core.wallet.holdings_state import load_state, state_path
        state = load_state(state_path(user_id=resolve_owner_user_id()))
    out = []
    for row in state.get("lost") or ():
        left = row.get("left") or row.get("left_while_ready") or {}
        sold = left.get("sold") if isinstance(left, dict) else None
        if not sold or not left.get("tx"):
            continue
        chain = str(row.get("chain") or "")
        net = int(sold.get("price") or 0) - int(sold.get("royalty") or 0)
        if net <= 0:
            continue
        currency = str(sold.get("currency") or "")
        out.append(IncomeEvent(
            key=f"nft:{chain}:{str(row.get('collection') or '').lower()}:{row.get('token_id')}:"
                f"{str(left['tx']).lower()}",
            kind="nft", at=float(row.get("lost_at") or 0),
            usd=_value_currency(chain, currency, net, price),
            detail=f"NFT sale {row.get('label') or row.get('token_id')} on {chain}, tx {left['tx']}"))
    return out


# ---------------------------------------------------------------------------- payments

def _ts(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:26], fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


async def payments(database, *, since: float) -> List[IncomeEvent]:
    """Completed offer and x402 payments on an income network, created in the 30 days before
    *since* or later. Raises when the store cannot be read."""
    from modules.x402.income_chains import income_predicate
    where, args = income_predicate()
    rows = await database.fetch_all(
        f"""SELECT id, amount_usd, chain, completed_at, updated_at
              FROM x402_payment_requests
             WHERE status = 'completed' AND {where}
               AND created_at >= datetime(?, 'unixepoch', '-30 day')""",
        (*args, float(since)))
    out = []
    for r in rows or ():
        r = dict(r)
        at = _ts(r.get("completed_at")) or _ts(r.get("updated_at"))
        if at is None:
            logger.info("income rule: payment %s has no readable time; not counted", r.get("id"))
            continue
        out.append(IncomeEvent(key=f"payments:{r['id']}", kind="payments", at=at,
                               usd=float(r.get("amount_usd") or 0),
                               detail=f"payment {r['id']} on {r.get('chain') or 'base'}"))
    return out


# ---------------------------------------------------------------------------- trading

def realized_totals(user_id: str, *, db_path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """``{chain:holder:token: {"usd", "label"}}`` — the realized totals of the treasury and of
    every NFT account. Raises when the book cannot be read."""
    from core.open_positions import realized_for
    out = {}
    for r in realized_for(user_id, db_path=db_path, all_accounts=True):
        holder = r.account or "treasury"
        out[f"{r.chain}:{holder}:{str(r.address).lower()}"] = {
            "usd": float(r.realized_usd),
            "label": f"{r.symbol or r.address} on {r.chain}"
                     + (f" in account {r.account}" if r.account else "")}
    return out


async def read_all(kinds: List[str], *, user_id: str, since: float, database=None
                   ) -> Tuple[List[IncomeEvent], Optional[Dict[str, Dict[str, Any]]], List[str]]:
    """Every reader the rule's kinds need: ``(events, realized totals or None, errors)``."""
    import asyncio
    events: List[IncomeEvent] = []
    totals = None
    errors: List[str] = []
    if "fees" in kinds:
        try:
            events += await asyncio.to_thread(fee_claims)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"fee claims unreadable ({exc})")
    if "nft" in kinds:
        try:
            events += await asyncio.to_thread(nft_sales)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"NFT sales unreadable ({exc})")
    if "payments" in kinds:
        try:
            if database is None:
                from modules.x402._db import resolve_db
                database = await resolve_db()
            if database is None:
                raise RuntimeError("no database")
            events += await payments(database, since=since)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"payments unreadable ({exc})")
    if "trading" in kinds:
        try:
            totals = await asyncio.to_thread(realized_totals, user_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"realized trading unreadable ({exc})")
    return events, totals, errors


__all__ = ["fee_claims", "nft_sales", "payments", "read_all", "realized_totals"]
