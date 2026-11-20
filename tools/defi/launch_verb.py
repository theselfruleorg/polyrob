"""``defi_trade.launch_token`` — a launch with a quote token (proposal 122 W3).

A thin verb over three existing ones, in one act and in this order:

1. ``deploy_token`` — the pinned fixed-supply template (``deploy_verb``), marked
   ``launch=("deploy", quote)`` so the guard counts it against ``launch.per_day``;
2. the v4 declaration of proposal 117 (``core.wallet.lp_seed``) on token/quote, CONFIRMED by
   this act's own approval — the owner's card for this call, a mandate that names this verb,
   or a genuine owner turn. Its origin is ``launch``, so the seed carries the launch mark;
3. per ERC-20 leg, the ERC-20 allowance to Permit2 and the exact Permit2 grant
   (``approve_token``), then ``lp_add(protocol='v4', seed=<ref>)`` — the 117 seed path,
   unchanged. There is no second seed path.

Inside step 3 the new token has no market price: its price is the launch's own
(``initial_price`` x the quote's spend-grade price), bound for this call only
(:func:`bound_price`), so the caps value the grants and the seed at the price the act states.
The Permit2 grants are ``lp_v4_seed.make_grants``, the path of every confirmed
declaration (the pinned Permit2 is a route spender). The guard judges every
transaction (one authorizer); a launch adds refusals, never an allow.

The owner pin ``launch.pair`` and the ceilings ``launch.per_day`` / ``launch.max_pool_usd``
are read here first (a refusal names the rule before anything is sent) and again by the
guard (``core.wallet.launch_policy``, step 7a).
"""
from __future__ import annotations

import contextvars
import logging
from types import SimpleNamespace
from typing import Optional

logger = logging.getLogger(__name__)

#: ``{(chain, token): usd}`` — the launch price of the token THIS call deployed.
_PRICE: contextvars.ContextVar = contextvars.ContextVar("launch_price", default=None)


def bound_price(chain, addr) -> Optional[float]:
    """The launch price of a token this launch deployed, inside this call only; else None."""
    table = _PRICE.get()
    if not table:
        return None
    return table.get((str(chain or "").lower(), str(addr or "").lower()))



def settings():
    """The ``launch.*`` owner preferences — ``core.wallet.launch_policy.read_owner_settings``."""
    from core import prefs as _prefs
    from core.wallet import launch_policy
    return launch_policy.read_owner_settings(
        _prefs.owner_pref_scope, _prefs.load_preferences, _prefs.resolve)


def _quote(text: str) -> str:
    q = str(text or "").strip().lower()
    if len(q) != 42 or not q.startswith("0x"):
        raise ValueError("quote_token is a contract address, or the zero address for the "
                         "native coin")
    bytes.fromhex(q[2:])
    return q


def _owner_approved(ctx, tool, max_spend_usd: float) -> bool:
    """This call carries the owner's approval: a genuine owner turn, or the owner's grant
    for THIS call (the card's Confirm, or a mandate — ``self_transfer.mandate_exemption``
    stamps it) covering the declared bound."""
    if ctx is None:
        return False
    from core.money.authority import owner_direct_turn
    from core.wallet import tx_guard
    from tools.controller.turn_origin import _is_forged_or_autonomous_turn
    try:
        if owner_direct_turn(ctx, _is_forged_or_autonomous_turn, tool):
            return True
    except Exception:  # noqa: BLE001
        return False
    return tx_guard._owner_grant_covers(ctx, SimpleNamespace(max_spend_usd=max_spend_usd))


def _token_usd(tool, chain: str, quote: str, initial_price: float) -> Optional[float]:
    """USD of ONE new token at the launch price, or None (the quote has no spend price)."""
    from core.wallet import chains
    from core.wallet.launch_policy import NATIVE
    ref = chains.get(chain).wrapped_native if quote == NATIVE else quote
    try:
        px = float(tool._price(chain, ref) or 0)
    except Exception:  # noqa: BLE001
        return None
    return px * float(initial_price) if px > 0 else None


def preflight(tool, p, gate, s, *, now=None) -> Optional[str]:
    """The refusals before anything is sent (the pin, the ceilings, the LP caps)."""
    from core.wallet import dex_registry, launch_policy as L
    from tools.defi import lp_v4_verbs
    row = dex_registry.row_for(p.chain, "v4")
    if row is None or not row.position_manager or not row.permit2:
        return f"refused: {p.chain} has no pinned Uniswap v4 deployment to seed a pool on"
    quote = _quote(p.quote_token)
    why = L.pair_refusal(s, p.chain, (quote,))
    if why:
        return why
    try:
        done = L.gate_launches_today(gate, now)
    except Exception as exc:  # noqa: BLE001
        return f"refused: {exc}"
    why = L.count_refusal(s, done)
    if why:
        return why
    if quote == L.NATIVE and p.quote_amount > 0:
        from decimal import Decimal
        wei = int(Decimal(str(p.quote_amount)) * 10 ** 18)
        try:
            from core.wallet.onchain import _rpc, rpc_url_for_chain
            balance = int(_rpc(rpc_url_for_chain(p.chain), "eth_getBalance",
                               [tool._get_wallet().operational_signer().address, "latest"],
                               timeout=8.0), 16)
        except Exception as exc:  # noqa: BLE001
            return f"refused: the native balance could not be read ({exc})"
        why = lp_v4_verbs.caps_refusal(gate, wei, balance)
        if why:
            return f"refused: {why}"
    return None


def card_context(params: dict) -> list:
    """The approval card of a launch: the pin, the ceilings and the LP caps (122 W3)."""
    from core.wallet import launch_policy as L
    from tools.defi.lp_v4_verbs import lp_caps
    s = settings()
    try:
        from core.wallet.factory import get_agent_wallet
        w = get_agent_wallet()
        done = L.gate_launches_today(w.policy) if w is not None else None
    except Exception:  # noqa: BLE001 — shown as "?"
        done = None
    lines = ["• What this one approval does: deploy the token, confirm its v4 pool declaration, "
             "make the Permit2 grants of each token leg, and seed the pool. Each transaction "
             "still meets the money guard and your limits."]
    lines += ["• " + ln for ln in L.facts_lines(s, done_today=done, lp_caps=lp_caps())]
    try:
        q = _quote(params.get("quote_token"))
        why = L.pair_refusal(s, str(params.get("chain") or ""), (q,))
        if why:
            lines.append("⚠️ " + why)
    except ValueError as exc:
        lines.append(f"⚠️ {exc}")
    return lines


async def perform_launch_token(tool, p, ctx=None):
    from core.wallet import launch_policy as L, lp_seed as S
    from tools.defi import deploy_verb, lp_v4_seed
    from tools.defi.lp_verbs import liquidity_enabled
    if not deploy_verb.deploy_enabled():
        return tool._ar(error=f"a launch deploys a token, and deployment is off "
                              f"({deploy_verb.FLAG}). Nothing was sent.")
    if not liquidity_enabled():
        return tool._ar(error="a launch seeds a pool, and liquidity writes are off. Nothing "
                              "was sent.")
    wallet = tool._get_wallet()
    if wallet is None:
        from core.wallet.config import WALLET_OFF_TEXT
        return tool._ar(error=WALLET_OFF_TEXT)
    s = settings()
    try:
        why = preflight(tool, p, wallet.policy, s)
        quote = _quote(p.quote_token)
    except ValueError as exc:
        why = f"refused: {exc}"
    if why:
        return tool._ar(error=why + ". Nothing was sent.")
    if not p.dry_run and not _owner_approved(ctx, tool, p.max_spend_usd):
        # 126 wave 3: the owner's act is ONE card holding this exact launch.
        from tools.controller.call_card_run import owner_lane_card
        card = await owner_lane_card(tool, ctx, title="Launch a token",
                                     why="A launch is the owner's act.")
        if card is not None:
            return card
        return tool._ar(error=("refused: a launch is the owner's act — it runs on the owner's "
                               "card for this call, a mandate that names defi_trade_launch_token "
                               "and this quote token, or an owner turn. Nothing was sent."))
    unit_usd = _token_usd(tool, p.chain, quote, p.initial_price)
    if unit_usd is None:
        return tool._ar(error=(f"refused: the quote token {quote} has no spend-grade price, so the "
                               f"seed cannot be valued against max_spend_usd and the limits. "
                               f"Nothing was sent."))
    from tools.defi.trade_params import DeployTokenParams
    deploy = DeployTokenParams(chain=p.chain, name=p.name, symbol=p.symbol, supply=p.supply,
                               decimals=18, max_spend_usd=p.max_spend_usd,
                               dry_run=p.dry_run)
    seed_usd = float(p.token_amount) * unit_usd + float(p.quote_amount) * unit_usd / p.initial_price
    plan = (f"launch {p.symbol.strip()!r} on {p.chain}, paired to {quote}\n"
            f"  seed: {p.token_amount:g} {p.symbol.strip()} + {p.quote_amount:g} quote at "
            f"{p.initial_price:g} quote per token (fee {p.fee}, spacing {p.tick_spacing}, "
            f"range {p.range}) — about ${seed_usd:,.2f} at the launch price\n")
    if seed_usd > p.max_spend_usd:
        return tool._ar(error=(plan + f"refused: the seed is worth about ${seed_usd:,.2f} at the "
                               f"launch price, above max_spend_usd ${p.max_spend_usd:,.2f}. "
                               f"Nothing was sent."))
    why = L.seed_refusal(s, seed_usd)
    if why:
        return tool._ar(error=plan + why + ". Nothing was sent.")
    out: dict = {}
    first = await deploy_verb.perform_deploy_token(tool, deploy, ctx, launch=("deploy", quote),
                                                   out=out)
    if p.dry_run or first.error or not out.get("address"):
        tail = ("  next (after the deploy lands): the v4 declaration, the Permit2 grants and the "
                "seed, in this same call with dry_run=false\n" if p.dry_run else "")
        if first.error:
            return tool._ar(error=plan + first.error)
        return tool._ar(content=plan + (first.extracted_content or "") + "\n" + tail)
    token = out["address"]
    report = [plan + (first.extracted_content or "")]
    return await _declare_and_seed(tool, p, ctx, wallet, token, quote, unit_usd, report, S,
                                   lp_v4_seed)


async def _declare_and_seed(tool, p, ctx, wallet, token, quote, unit_usd, report, S, lp_v4_seed):
    from tools.defi.lp_verbs import perform_lp_add
    from tools.defi.trade_params import LpAddParams
    uid = lp_v4_seed._uid(ctx)
    try:
        from core.wallet.broadcast.evm import EvmRail
        rpc = getattr(tool, "_lp_rpc", None) or (tool._rail_factory or EvmRail)(
            chain=p.chain, signer=wallet.operational_signer())._rpc
        ns = SimpleNamespace(chain=p.chain, token_id=None, token_a=token, token_b=quote,
                             amount_a=p.token_amount, amount_b=p.quote_amount,
                             initial_price=p.initial_price, fee=p.fee,
                             tick_spacing=p.tick_spacing, hooks=None, range=p.range,
                             slippage_bps=S.GAP_DEFAULT_BPS)
        fields = lp_v4_seed.build_fields(ns, rpc, source="agent")
        ref, f = S.propose(uid, fields, origin="launch")
        S.confirm(ref, user_id=uid)
    except Exception as exc:  # noqa: BLE001
        report.append(f"STOPPED at the pool declaration: {exc}. The token is deployed at {token}; "
                      f"nothing else was sent. Finish with lp_add(protocol='v4', …) and the "
                      f"owner's card.")
        return tool._ar(error="\n".join(report))
    report.append(f"declaration {ref}: CONFIRMED by this launch's approval (one use, one hour)\n"
                  + "\n".join(S.summary_lines(f)))
    run = LpAddParams(chain=p.chain, protocol="v4", seed=ref, max_spend_usd=p.max_spend_usd,
                      dry_run=False)
    price_tok = _PRICE.set({(p.chain.lower(), token.lower()): unit_usd})
    try:
        # The Permit2 legs and the mint: the one path every confirmed declaration takes
        # (perform_lp_add makes the grants through lp_v4_seed.make_grants).
        seeded = await perform_lp_add(tool, run, ctx)
    finally:
        _PRICE.reset(price_tok)
    if seeded.error:
        # An error, never content: a card of this launch must not read done with no pool.
        report.append(f"STOPPED at the seed: {seeded.error}\nThe token is deployed at {token}; "
                      f"declaration {ref} stays CONFIRMED for one hour. Finish: lp_add(chain="
                      f"'{p.chain}', protocol='v4', seed='{ref}', max_spend_usd=…, dry_run=false).")
        return tool._ar(error="\n".join(report))
    report.append(seeded.extracted_content or "")
    return tool._ar(content="\n".join(r for r in report if r))



__all__ = ["bound_price", "card_context", "perform_launch_token", "preflight",
           "settings"]
