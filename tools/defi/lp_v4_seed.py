"""A v4 position on a PoolKey the OWNER declared (117): create the pool and mint
its first position in one transaction, or mint a custom range on that pool.

Two entries of the one verb, ``defi_trade.lp_add(protocol='v4', …)``:

* **a PoolKey named in full** (``tick_spacing`` given, with ``fee``, ``range``,
  both amounts and ``initial_price``) sends nothing. It reads the chain, stores
  a PROPOSED declaration (``core.wallet.lp_seed``) and puts ONE card to the
  owner. Confirm runs ``/lp seed confirm <ref>`` on the owner's seat.
* **``seed=<ref>``** builds the transaction from the CONFIRMED row and from
  nothing the caller says: ``multicall([initializePool, modifyLiquidities])``
  while the pool has no price, the plain mint when it has one. A dry run also
  reads a row that is not confirmed yet, so the plan is seen before the card.

The price the owner confirmed is the anchor. A pool that exists must be within
the declared gap of it; the signed maxima hold the mint to that price
(``liquidity_guard._assert_price_bound``). The grants are the existing verbs:
the ERC-20 allowance to Permit2 (owner turn), then
``approve_token(via='permit2')``; a short one refuses with the exact call.
"""
import logging
import time
from decimal import Decimal
from typing import Any, Dict, Optional

from core.wallet import chains, dex_registry, lp_seed as S, tx_guard, univ3_math as M
from tools.defi import lp_reads as R, lp_v4 as V
from tools.defi.lp_v4_verbs import DEADLINE_S, legs_ready

logger = logging.getLogger(__name__)

_SYMBOL = {"name": "symbol", "inputs": [], "outputs": [{"type": "string"}]}
#: The seed is built at the signed price itself: the maxima carry rounding only.
_ROUNDING_BPS = 1


def proposes(p) -> bool:
    """True when this ``lp_add`` names a v4 PoolKey in full and no declaration."""
    return (p.protocol == "v4" and getattr(p, "seed", None) is None
            and getattr(p, "tick_spacing", None) is not None)


def _symbol(rpc, chain: str, currency: str) -> str:
    if currency == V.ZERO:
        return chains.get(chain).native_symbol or "native"
    try:
        return str(R.view(rpc, currency, _SYMBOL)) or "?"
    except Exception:
        return "?"


def build_fields(p, rpc, *, source: str) -> Dict[str, Any]:
    """The declaration fields of an ``lp_add`` that names a PoolKey in full,
    every on-chain fact read now. Raises ``ValueError`` with what is missing."""
    from core.wallet.tokens import raw_amount
    dex_registry.resolve_position_manager(p.chain, "v4")
    if p.token_id is not None:
        raise ValueError("a declaration mints a NEW position; token_id is not accepted")
    if p.token_a is None or p.token_b is None or p.amount_a is None or p.amount_b is None:
        raise ValueError("a declaration needs token_a, token_b, amount_a and amount_b")
    if p.initial_price is None:
        raise ValueError("a declaration needs initial_price (token_b per token_a): the price "
                         "the owner confirms, for a new pool and for an existing one")
    key, pid, flipped = V.explicit_key(p.token_a, p.token_b, p.fee, p.tick_spacing, p.hooks)
    c0, c1 = key["currency0"], key["currency1"]
    dec0 = chains.get(p.chain).native_decimals if c0 == V.ZERO else R.decimals(rpc, p.chain, c0)
    dec1 = R.decimals(rpc, p.chain, c1)
    lo, hi = V.range_ticks(p.range, key["tickSpacing"])
    price = float(p.initial_price)
    sqrt = M.sqrt_price_from_price(1 / price if flipped else price, dec0, dec1)
    h0, h1 = (p.amount_b, p.amount_a) if flipped else (p.amount_a, p.amount_b)
    return {"chain": p.chain, "currency0": c0, "currency1": c1, "fee": key["fee"],
            "tick_spacing": key["tickSpacing"], "hooks": key["hooks"], "pool_id": pid,
            "tick_lower": lo, "tick_upper": hi,
            "amount0_raw": raw_amount(h0, dec0), "amount1_raw": raw_amount(h1, dec1),
            "dec0": dec0, "dec1": dec1, "sym0": _symbol(rpc, p.chain, c0),
            "sym1": _symbol(rpc, p.chain, c1), "sqrt_price_x96": sqrt,
            "price_source": source,
            "gap_bps": max(1, min(int(p.slippage_bps), S.GAP_MAX_BPS))}


def _drift_bps(sqrt_now: int, sqrt_anchor: int) -> float:
    return abs((sqrt_now / sqrt_anchor) ** 2 - 1) * 10000


def _notes(f, rpc, price_fn) -> tuple:
    """What the card adds to the declaration: the pool's state now, the
    independent price when both legs have one, and the value in USD."""
    notes = []
    try:
        st = V.pool_state(rpc, f["chain"], f["pool_id"])
        if st.sqrt_price_x96 == 0:
            notes.append("the pool does NOT exist yet: the transaction creates it at this "
                         "price and mints the position, together or not at all")
        else:
            notes.append(f"the pool EXISTS; its price now is "
                         f"{_drift_bps(st.sqrt_price_x96, int(f['sqrt_price_x96'])) / 100:.2f}% "
                         f"away from this price")
    except Exception as exc:
        notes.append(f"pool state: NOT READ ({exc})")
    try:
        wrapped = chains.get(f["chain"]).wrapped_native
        p0 = float(price_fn(f["chain"], wrapped if f["currency0"] == V.ZERO else f["currency0"]))
    except Exception:
        p0 = None
    if p0 and p0 > 0:
        a0 = int(f["amount0_raw"]) / 10 ** f["dec0"]
        value = a0 * p0 * (2 if int(f["amount1_raw"]) else 1)
        notes.append(f"value: about ${value:,.2f} at ${p0:,.2f} per {f['sym0']} (the "
                     f"{f['sym1']} leg valued through this price)")
        try:
            p1 = float(price_fn(f["chain"], f["currency1"]))
        except Exception:
            p1 = None
        if p1 and p1 > 0:
            mine = S.price_of(f["sqrt_price_x96"], f["dec0"], f["dec1"])
            notes.append(f"independent price: 1 {f['sym0']} = {p0 / p1:.10g} {f['sym1']} — this "
                         f"declaration is {abs(mine / (p0 / p1) - 1) * 100:.2f}% away from it")
        else:
            notes.append(f"{f['sym1']} has NO independent price: nothing checks this number "
                         f"but you")
    return tuple(notes)


async def raise_card(tool, p, ctx, rpc):
    """Store the proposed declaration and put its card to the owner. Sends no
    transaction and writes nothing but the PROPOSED row."""
    from core.surfaces import cards
    user_id = _uid(ctx)
    try:
        from core.instance import is_owner_uid
        owner = is_owner_uid(user_id)
    except Exception:
        owner = False
    if not owner:
        return tool._ar(error="refused: a pool declaration goes to the owner, and this "
                              "tenant is not the owner. Nothing was proposed.")
    seat = (getattr(ctx, "metadata", None) or {}).get("owner_seat")
    try:
        fields = build_fields(p, rpc, source="owner" if seat else "agent")
        ref, f = S.propose(user_id, fields, origin="owner_seat" if seat else "agent")
        sid = str(getattr(ctx, "session_id", "") or "") or None
        card = S.seed_card(user_id, ref, f, origin="owner_seat" if seat else "agent",
                           session_id=sid, notes=_notes(f, rpc, tool._price))
    except (S.SeedError, V.LpReadError, ValueError) as exc:
        return tool._ar(error=f"refused: {exc}. Nothing was proposed and nothing was sent.")
    outcome = "failed"
    try:
        outcome = await cards.push(getattr(tool, "container", None), user_id, card, session_id=sid)
    except Exception as exc:
        logger.warning("pool declaration card %s: delivery failed: %s", card.card_id, exc)
    where = ("on the owner's chat" if outcome in ("sent", "queued")
             else "in the owner's /cards list (no live chat reached)")
    return tool._ar(content=(
        f"NOT sent: this is a pool the owner must declare first. Declaration {ref} is "
        f"PROPOSED; card {card.card_id} is {where}.\n" + "\n".join(S.summary_lines(f)) + "\n"
        f"See the plan now with lp_add(chain='{p.chain}', protocol='v4', seed='{ref}', "
        f"max_spend_usd=…, dry_run=true). After the owner confirms, the same call with "
        f"dry_run=false sends it. Do not say a pool exists until that call reports success."))


def _uid(ctx) -> str:
    uid = getattr(ctx, "user_id", None)
    if uid:
        return str(uid)
    from core.identity import resolve_identity
    return str(resolve_identity())


def load(p, ctx) -> Dict[str, Any]:
    """The declaration row of ``p.seed`` for this caller. A live call needs a
    CONFIRMED row; a dry run also reads a PROPOSED one."""
    try:
        row = S.get(p.seed, user_id=_uid(ctx))
    except S.SeedUnreadable as exc:
        raise ValueError(f"{exc}; a declaration that cannot be read is not 'no declaration'")
    f = row["fields"]
    if f["chain"] != p.chain:
        raise ValueError(f"declaration {p.seed} is for {f['chain']}, not {p.chain}")
    ok = (S.CONFIRMED,) + ((S.PROPOSED,) if p.dry_run else ())
    if row["state"] not in ok:
        hint = (" — the owner confirms its card first (/cards)" if row["state"] == S.PROPOSED
                else "")
        raise ValueError(f"declaration {p.seed} is {row['state'].upper()}{hint}")
    return row


def prepare_seed_v4(p, rpc, holder, npm, row):
    """The plan of ``lp_add(protocol='v4', seed=<ref>)``. Everything but
    ``max_spend_usd`` and ``dry_run`` comes from the declaration row."""
    from core.wallet import liquidity_guard as G
    from tools.defi.lp_verbs import Plan
    f = row["fields"]
    key, pid = S.key_of(f), f["pool_id"]
    dex_registry.verify_hook(rpc, p.chain, key["hooks"])
    st = V.pool_state(rpc, p.chain, pid)
    anchor, gap = int(f["sqrt_price_x96"]), int(f["gap_bps"])
    lo, hi = f["tick_lower"], f["tick_upper"]
    sa, sb = M.sqrt_price_at_tick(lo), M.sqrt_price_at_tick(hi)
    declared = [int(f["amount0_raw"]), int(f["amount1_raw"])]
    creates = st.sqrt_price_x96 == 0
    if creates:
        sqrt, slack = anchor, _ROUNDING_BPS
        how = "the pool has no price: this transaction creates it at the confirmed price"
    else:
        sqrt, slack = st.sqrt_price_x96, gap
        drift = _drift_bps(sqrt, anchor)
        if drift > gap:
            raise ValueError(
                f"v4 pool {pid} exists and its price is {drift / 100:.2f}% away from the price "
                f"the owner confirmed (allowed {gap / 100:g}%). The anchor does not move: the "
                f"owner declares the new price with a new card")
        how = f"the pool exists; its price is {drift / 100:.2f}% from the confirmed price"
    if row["state"] != S.CONFIRMED:
        how += ("\nNOT CONFIRMED: this is a plan only. A live send needs the owner's Confirm "
                "on the declaration's card")
    liq = M.liquidity_for_amounts(sqrt, sa, sb, *(d * (10000 - slack) // 10000 for d in declared))
    if not 0 < liq < 2 ** 128:
        raise ValueError("at the price now the declared amounts make no liquidity")
    used = M.amounts_for_liquidity(sqrt, sa, sb, liq)
    maxima = [min(d, u * (10000 + slack) // 10000 + 1) if u else 0 for d, u in zip(declared, used)]
    c0, c1 = key["currency0"], key["currency1"]
    now = int(time.time())
    out, held = legs_ready(p.chain, rpc, holder, npm,
                           ((c0, maxima[0], f["dec0"]), (c1, maxima[1], f["dec1"])), now)
    unlock = V.encode_mint_unlock(key, lo, hi, liq, maxima[0], maxima[1], holder)
    data = V.encode_modify_liquidities(unlock, now + DEADLINE_S)
    reg = dex_registry.row_for(p.chain, "v4")
    events = [(reg.pool_manager, V.TOPIC_MODIFY_LIQUIDITY)]
    if creates:
        data = V.encode_seed_multicall(key, anchor, data)
        events.append((reg.pool_manager, G.TOPIC_V4_INITIALIZE))
    value = maxima[0] if c0 == V.ZERO else 0
    intent = tx_guard.TxIntent(
        chain=p.chain, token=None, to=npm, amount_raw=0, max_spend_usd=p.max_spend_usd,
        is_liquidity_op=True, lp_outflows=tuple(out), lp_held_balances=tuple(held),
        watch_spenders=(reg.permit2,), lp_position=(npm, None), lp_position_effect="mint",
        expected_events=tuple(events), idempotency_key=None)
    amt = lambda n, d: f"{Decimal(n) / Decimal(10) ** d:f}"
    plan = Plan(intent, data, value, pid, (c0, c1), (f["dec0"], f["dec1"]),
                f"declaration {p.seed} ({row['state']}): " + "; ".join(S.summary_lines(f)[:3]) + "\n"
                f"{how}\n"
                f"ticks [{lo}, {hi}]; liquidity {liq}; at most {amt(maxima[0], f['dec0'])} "
                f"{f['sym0']} + {amt(maxima[1], f['dec1'])} {f['sym1']} (raw {maxima})\n")
    plan.seed = {"ref": p.seed, "fields": f, "liquidity": liq, "creates": creates,
                 "state": row["state"]}
    return plan


def claim(seed: Dict[str, Any]) -> None:
    """Take the declaration's one use, or raise: nothing is signed without it."""
    if seed["state"] != S.CONFIRMED or not S.claim(seed["ref"]):
        raise ValueError(f"declaration {seed['ref']} is not CONFIRMED any more (used, expired, "
                         f"or still waiting for the owner's Confirm)")


def unsent(seed: Dict[str, Any], *, unknown: bool, why: str) -> None:
    """The signature or the broadcast failed after the claim. A known "not
    sent" gives the use back; an unknown outcome closes the declaration."""
    try:
        if unknown:
            S.finish(seed["ref"], S.FAILED, note=f"broadcast outcome unknown: {why}")
        else:
            S.release(seed["ref"], note=f"not sent: {why}")
    except Exception:
        pass


def settle(seed: Dict[str, Any], rpc, chain: str, *, tx_hash: str, holder: str,
           token_id: Any, reverted: bool) -> str:
    """Close the declaration after the receipt and return the lines the result
    adds. A store error never hides the transaction's own result."""
    ref = seed["ref"]
    try:
        if reverted:
            S.release(ref, note=f"reverted: {tx_hash}")
            return (f"\nThe transaction reverted: nothing moved and no pool was created. "
                    f"Declaration {ref} is CONFIRMED again until it expires.")
        if token_id is None:
            S.finish(ref, S.FAILED, tx_hash=tx_hash, note="receipt not verified")
            return (f"\nDeclaration {ref} is closed as FAILED: the receipt was not verified. "
                    f"Read lp_pool_info and lp_positions (protocol='v4') before a new one.")
        S.finish(ref, S.SEEDED, token_id=token_id, tx_hash=tx_hash)
    except Exception as exc:
        return f"\nDeclaration {ref}: its state was NOT recorded ({exc})."
    return (f"\ndeclaration {ref}: SEEDED. Read back from the chain:\n"
            + "\n".join(verify_lines(rpc, chain, seed, token_id, holder)))


def verify_lines(rpc, chain: str, seed: Dict[str, Any], token_id: Any, holder: str) -> list:
    """117 §4.5: the landed position and pool, read back. A read that fails
    prints UNVERIFIED for its line; a mismatch says MISMATCH, never success."""
    f = seed["fields"]
    lines = []

    def check(label, read, want=None, fmt=str):
        try:
            got = read()
        except Exception as exc:
            lines.append(f"  {label}: UNVERIFIED ({exc})")
            return None
        if want is not None and got != want:
            lines.append(f"  {label}: MISMATCH — chain says {fmt(got)}, the declaration says {fmt(want)}")
        else:
            lines.append(f"  {label}: {fmt(got)}" + (" — verified" if want is not None else ""))
        return got

    pos = None
    try:
        pos = V.position(rpc, chain, int(token_id))
    except Exception as exc:
        lines.append(f"  position #{token_id}: UNVERIFIED ({exc})")
    if pos is not None:
        check("owner", lambda: pos.owner, holder.lower())
        check("pool id", lambda: pos.pool_id, f["pool_id"])
        check("ticks", lambda: (pos.tick_lower, pos.tick_upper),
              (f["tick_lower"], f["tick_upper"]))
        check("liquidity", lambda: pos.liquidity, seed["liquidity"])

    def price_gap():
        st = V.pool_state(rpc, chain, f["pool_id"])
        if st.sqrt_price_x96 == 0:
            raise ValueError("the pool reads as NOT initialized")
        drift = _drift_bps(st.sqrt_price_x96, int(f["sqrt_price_x96"]))
        if drift > f["gap_bps"]:
            raise ValueError(f"price is {drift / 100:.2f}% from the confirmed price")
        return f"{drift / 100:.2f}% from the confirmed price (tick {st.tick})"
    check("pool price", price_gap)
    reg = dex_registry.row_for(chain, "v4")
    for cur, sym in ((f["currency0"], f["sym0"]), (f["currency1"], f["sym1"])):
        if cur == V.ZERO:
            continue
        check(f"Permit2 grant left on {sym}",
              lambda c=cur: V.permit2_allowance(rpc, chain, holder, c, reg.position_manager)[0])
        check(f"ERC-20 allowance to Permit2 left on {sym}",
              lambda c=cur: R.allowance(rpc, chain, c, holder, reg.permit2))
    return lines


__all__ = ["build_fields", "claim", "load", "prepare_seed_v4", "proposes", "raise_card",
           "settle", "unsent", "verify_lines"]
