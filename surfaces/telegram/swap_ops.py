"""``/swap`` — the owner swaps one token for another, from chat.

Why (crypto-UX review, 2026-09-27): a swap is the most common money action and
no seat had a verb for it — the owner could only ask the agent in prose
(``/trade``), and the prose, not a quote, decided what happened.

Same shape as ``/send`` (``send_ops``):

* bare ``/swap …`` QUOTES — the guarded dry run: the route, the quoted and
  minimum output, the simulated value, the caps — and becomes an action card
  whose Confirm runs the confirm line once, bounded at the quote + 5%;
* ``/swap … go`` swaps. A genuine owner turn: the pause and the autonomous
  ceiling do not bind it; the hard caps, the slippage floor, the route check,
  the token-identity gate and the simulation do.

⚠️ REACH, never policy: every gate is inside ``defi_trade.swap`` /
``solana_swap`` and ``tx_guard``. This module parses and renders only. An
ERC-20 you sell needs an exact allowance first; the rail says so and names it.
"""
from __future__ import annotations

import logging
import re
from typing import Any, List, Optional, Tuple

from surfaces.telegram.send_ops import (
    _EVM_ADDR, _GO, _PRICE_ROOM, _SOL_ADDR, _caps_line, _checksum_ok, _owner_ctx,
    _owner_words, _render,
)

logger = logging.getLogger(__name__)

USAGE = ("Usage: /swap <amount> <native|token-address> to <token-address> on <chain> "
         "[slippage <bps>] [max <usd>] [go]\n"
         "e.g. /swap 0.1 native to 0x8335…2913 on base           — quote only\n"
         "     /swap 0.1 native to 0x8335…2913 on base go        — swap\n"
         "     /swap 50 EPjF…t1v to native on solana slippage 50 — quote\n\n"
         "The quote shows the route, the minimum you receive, the value and the "
         "caps, with a Confirm button that swaps exactly that quote (at most the "
         "quote + 5%). Tickers are never enough: name the contract (or mint). "
         "Selling an ERC-20 needs an exact allowance first — the quote says so.")

#: Solana's native mint, the one ``native`` means on solana.
_WSOL = "So11111111111111111111111111111111111111112"


def parse(args: List[str]) -> Tuple[Optional[dict], Optional[str]]:
    """``(order, None)`` or ``(None, error)``. Keys: amount, token_in, token_out,
    chain, slippage_bps, max_usd, go."""
    words = [w for w in (args or []) if w]
    go = bool(words) and words[-1].lower() in _GO
    if go:
        words = words[:-1]
    chain = out = None
    slippage = None
    max_usd = None
    rest: List[str] = []
    i = 0
    while i < len(words):
        w = words[i].lower()
        nxt = words[i + 1] if i + 1 < len(words) else None
        if w == "on" and nxt:
            chain = nxt.lower()
        elif w == "to" and nxt:
            out = nxt
        elif w == "slippage" and nxt:
            try:
                slippage = int(nxt.rstrip("bps"))
            except ValueError:
                return None, f"`slippage` is basis points (100 = 1%), got {nxt!r}."
            if not (1 <= slippage <= 1000):
                return None, "`slippage` must be 1-1000 basis points."
        elif w == "max" and nxt:
            try:
                max_usd = float(nxt.lstrip("$").replace(",", ""))
            except ValueError:
                return None, f"`max` needs a USD number, got {nxt!r}."
            if not (max_usd > 0):
                return None, "`max` must be above zero."
        else:
            rest.append(words[i])
            i += 1
            continue
        i += 2
    if len(rest) != 2:
        return None, USAGE
    try:
        amount = float(rest[0])
    except ValueError:
        return None, f"Amount must be a positive number, got {rest[0]!r}.\n\n{USAGE}"
    if not (amount > 0):
        return None, f"Amount must be a positive number, got {rest[0]!r}."
    token_in = rest[1]
    if not out:
        return None, "Name what you buy: `to <token-address>` (or `to native`).\n\n" + USAGE
    if not chain:
        return None, "Name the chain: `on <chain>` — I never guess.\n\n" + USAGE
    solana = chain == "solana"
    shape = _SOL_ADDR if solana else _EVM_ADDR
    for label, tok in (("sell", token_in), ("buy", out)):
        if tok.lower() == "native":
            continue
        if not shape.match(tok):
            kind = "mint" if solana else "contract"
            return None, (f"{tok!r} is not a {kind} address (what you {label}). A "
                          f"ticker is never enough — name the {kind}, or `native`.")
        if not solana and not _checksum_ok(tok):
            return None, (f"{tok!r} fails its checksum (mixed case that does not "
                          f"match the address). Copy it again. Nothing was swapped.")
    if token_in.lower() == out.lower():
        return None, "You would sell and buy the same token."
    return {"amount": amount, "token_in": token_in, "token_out": out, "chain": chain,
            "slippage_bps": slippage, "max_usd": max_usd, "go": go}, None


_VALUE_RE = re.compile(r"(?:simulated value|valued):\s*\$([0-9][0-9,]*\.?[0-9]*)")


def _quoted_usd(result) -> Optional[float]:
    # The LAST match: the header prints token symbols (text a contract
    # controls) before the guard's own value line.
    found = _VALUE_RE.findall(getattr(result, "extracted_content", None) or "")
    if not found:
        return None
    try:
        return float(found[-1].replace(",", ""))
    except ValueError:
        return None


def _is_dry_run(result) -> bool:
    body = getattr(result, "extracted_content", None) or ""
    return "DRY RUN" in body and not getattr(result, "error", None)


async def _run(tool, order: dict, ctx, *, dry_run: bool, max_spend_usd: float):
    if order["chain"] == "solana":
        from tools.defi.trade_tool import SolanaSwapParams
        tin = _WSOL if order["token_in"].lower() == "native" else order["token_in"]
        tout = _WSOL if order["token_out"].lower() == "native" else order["token_out"]
        params = SolanaSwapParams(token_in=tin, token_out=tout, amount_in=order["amount"],
                                  max_spend_usd=max_spend_usd,
                                  slippage_bps=order["slippage_bps"], dry_run=dry_run)
        return await tool.solana_swap(params, ctx)
    from tools.defi.trade_tool import SwapParams
    params = SwapParams(chain=order["chain"], token_in=order["token_in"],
                        token_out=order["token_out"], amount_in=order["amount"],
                        max_spend_usd=max_spend_usd,
                        slippage_bps=order["slippage_bps"], dry_run=dry_run)
    return await tool.swap(params, ctx)


def _confirm_args(args: List[str], order: dict, bound: float) -> List[str]:
    words = [w for w in args if w.lower() not in _GO]
    return words if order.get("max_usd") is not None else words + ["max", f"{bound:.2f}"]


async def swap_reply(user_id: Optional[str], args: List[str], *, tool: Any = None) -> str:
    """``/swap <amount> <in> to <out> on <chain> [slippage <bps>] [max <usd>] [go]``."""
    if not user_id:
        return "Only the owner can swap from the wallet."
    if not args:
        return USAGE
    order, err = parse(args)
    if err:
        return err
    if order["chain"] != "solana" and order["token_out"].lower() == "native":
        return ("Buying the native asset on an EVM chain is a sell into its wrapped "
                "token: name the wrapped token's address (e.g. WETH). Nothing was swapped.")
    try:
        from tools.defi.trade_tool import DefiTradeTool
    except Exception as exc:                      # pragma: no cover - import guard
        return f"The swap rail is unavailable: {exc}"
    tool = tool or DefiTradeTool()
    ctx = _owner_ctx(user_id)
    try:
        wallet = tool._get_wallet()
    except Exception:
        wallet = None
    if wallet is None:
        return ("❌ The agent wallet is not enabled on this instance, so there is "
                "nothing to swap from. Nothing was swapped.")
    try:
        cap = float(wallet.policy.per_tx_cap_usd)
    except Exception:
        return ("❌ The wallet is not readable, so I cannot hold this swap to a "
                "cap. Nothing was swapped.")
    caps = _caps_line(tool)
    try:
        quote = await _run(tool, order, ctx, dry_run=True, max_spend_usd=cap)
    except Exception as exc:
        logger.warning("/swap quote failed", exc_info=True)
        return f"❌ The quote did not run: {exc}. Nothing was swapped."
    if not _is_dry_run(quote):
        return _render(quote) + "\n" + caps
    usd = _quoted_usd(quote)
    if usd is None:
        return (_owner_words(_render(quote)) + "\n" + caps
                + "\n❌ The quote carried no USD value, so I cannot bound the swap. "
                  "Nothing was swapped.")
    bound = (min(cap, order["max_usd"]) if order.get("max_usd") is not None
             else min(cap, round(usd * _PRICE_ROOM + 0.01, 2)))
    if not order["go"]:
        return (_owner_words(_render(quote)) + "\n" + caps
                + f"  bound: the swap may be worth at most ${bound:,.2f} "
                  f"(the quote + 5% for the price to move)\n"
                + f"\nTo swap it: /swap {' '.join(_confirm_args(args, order, bound))} go")
    if usd > bound:
        return (f"❌ The price moved: the swap now simulates at ${usd:,.2f}, above the "
                f"${bound:,.2f} you confirmed. Nothing was swapped.\n" + caps)
    try:
        done = await _run(tool, order, ctx, dry_run=False, max_spend_usd=bound)
    except Exception as exc:
        logger.warning("/swap failed", exc_info=True)
        return (f"❌ The swap raised before a result: {exc}. Check /book before "
                "trying again — assume nothing about what happened.")
    return _render(done) + "\n" + caps


async def swap_verb(*, user_id: str, data_dir: str, args: List[str],
                    task_agent: Any = None, result: Any = None, board: Any = None) -> str:
    """Contributed-verb entry (``core.money_verbs``)."""
    return await swap_reply(user_id, list(args or []))
