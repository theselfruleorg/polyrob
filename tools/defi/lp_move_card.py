"""The ONE card of a v4 pool declaration: a new pool/range (a seed) or a position
move (proposal 126 waves 1–2).

A declaration used to be a card whose Confirm sent nothing, then a line to type
(``/lp seed run <ref> max <usd> go``), then an approval no seat could make. Now
the dry run is the card, and ONE Confirm runs two steps:

1. ``lp_seed_confirm`` — the owner's tap confirms the declaration (one use, one hour);
2. ``defi_trade_lp_add(seed=<ref>, dry_run=False, max_spend_usd=<bound>)`` — the
   Permit2 grants it lacks, then the mint (a move: the withdrawal, then the mint;
   ``lp_verbs.perform_lp_add``).

The bound is the value the dry run shows plus :data:`MARGIN` (the owner's answer,
2026-11-21); the guard holds the mint to it, so a price that moved more refuses and
the owner taps Refresh for a fresh dry run.
"""
import logging
import math
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

from tools.controller.call_card_run import MARGIN  # noqa: E402 — the one margin
REBUILD_KIND = "lp_declaration"
_NO_CAP = 1e12


def _caps(tool) -> tuple:
    try:
        gate = tool._get_wallet().policy
    except Exception:  # noqa: BLE001
        return None, None
    ok = lambda v: float(v) if isinstance(v, (int, float)) and v > 0 else None
    return ok(getattr(gate, "per_tx_cap_usd", None)), ok(getattr(gate, "daily_cap_usd", None))


def declared_usd(tool, chain: str, f: Dict[str, Any]) -> Optional[float]:
    """The declared amounts at independent prices, or None when a leg has none."""
    from core.wallet import chains
    zero = "0x" + "0" * 40
    usd = 0.0
    for cur, raw, dec in ((f["currency0"], f["amount0_raw"], f["dec0"]),
                          (f["currency1"], f["amount1_raw"], f["dec1"])):
        if not int(raw):
            continue
        try:
            px = float(tool._price(chain, chains.get(chain).wrapped_native if cur == zero
                                   else cur) or 0)
        except Exception:  # noqa: BLE001
            return None
        if not (math.isfinite(px) and px > 0):
            return None
        usd += int(raw) / 10 ** int(dec) * px
    return round(usd, 2)


def bound_for(value_usd: Optional[float], per_tx: Optional[float],
              daily: Optional[float]) -> Optional[float]:
    """The card's ``max_spend_usd``: the dry run's value + :data:`MARGIN`, or with no
    independent price the wallet's own per-transaction (else daily) limit."""
    if isinstance(value_usd, (int, float)) and value_usd > 0:
        return round(float(value_usd) * (1 + MARGIN) + 0.01, 2)
    return per_tx or daily


def _uid(ctx) -> str:
    from tools.defi.lp_v4_seed import _uid as uid
    return uid(ctx)


async def declaration_card(tool, ctx, chain: str, ref: str, f: Dict[str, Any], *,
                           summary: str = "") -> Any:
    """Dry-run declaration ``ref`` and put it to the owner as ONE call card."""
    from core.surfaces.money_report import owner_view
    from tools.controller.call_card_run import need_owner
    from tools.defi.lp_verbs import perform_lp_add
    from tools.defi.trade_params import LpAddParams
    move = "from_token_id" in f
    per_tx, daily = _caps(tool)
    dry = LpAddParams(chain=chain, protocol="v4", seed=ref, dry_run=True,
                      max_spend_usd=per_tx or _NO_CAP)
    res = await perform_lp_add(tool, dry, ctx)
    if res.error:
        return tool._ar(error=f"refused: the {'move' if move else 'pool'} does not pass its dry "
                              f"run, so no card was made (declaration {ref} stays PROPOSED):\n"
                              f"{res.error}")
    value = (getattr(res, "metadata", None) or {}).get("mint_usd") or declared_usd(tool, chain, f)
    bound = bound_for(value, per_tx, daily)
    if bound is None:
        return tool._ar(error="refused: the mint has no independent price and the wallet has "
                              "no limit to bound it, so no card was made. RESULT: NOT SENT.")
    said = (f"value about ${value:,.2f}; the guard holds the mint to ${bound:,.2f} "
            f"(+{MARGIN:.0%})" if value else
            f"no independent price: the guard holds the mint to the wallet limit ${bound:,.2f}")
    what = ("declare this move (one use, one hour), make the Permit2 grants it lacks, "
            "withdraw, mint" if move else
            "declare this pool and range (one use, one hour), make the Permit2 grants it "
            "lacks, mint")
    body = "\n".join((summary, owner_view(str(res.extracted_content or "")), "",
                      f"Confirm runs, once: {what}. {said}. The wallet's caps and LP_ETH_CAP "
                      f"still judge every transaction."))
    steps = [{"do": "lp_seed_confirm", "args": {"ref": ref}},
             {"do": "defi_trade_lp_add", "args": {
                 "chain": chain, "protocol": "v4", "seed": ref, "max_spend_usd": bound,
                 "dry_run": False}}]
    return await need_owner(
        tool, ctx, title="Move a pool position" if move else "Create a pool position",
        body=body, steps=steps,
        rebuild={"kind": REBUILD_KIND, "args": {"chain": chain, "ref": ref}},
        lead=f"Declaration {ref} is PROPOSED.", user_id=_uid(ctx))


async def rebuild(user_id: str, args: Dict[str, Any], *, seat: str, container: Any = None) -> str:
    """A Refresh: the same declaration, a fresh dry run, a new card."""
    from core.security.turn_facts import owner_seat_context
    from core.wallet import lp_seed
    from tools.controller.call_card_run import TOOL_FACTORIES
    from tools.defi.trade_tool import DefiTradeTool
    ref, chain = str(args.get("ref") or ""), str(args.get("chain") or "")
    factory = TOOL_FACTORIES.get("defi_trade")
    tool = factory() if factory else DefiTradeTool()
    if container is not None and getattr(tool, "container", None) is None:
        try:
            tool.container = container
        except Exception:  # noqa: BLE001
            pass
    row = lp_seed.get(ref, user_id=str(user_id))
    ctx = owner_seat_context(user_id, seat=seat)
    res = await declaration_card(tool, ctx, chain, ref, row["fields"],
                                 summary="\n".join(lp_seed.summary_lines(row["fields"])))
    return str(res.error or res.extracted_content or "")


__all__ = ["MARGIN", "bound_for", "declaration_card", "declared_usd", "rebuild"]
