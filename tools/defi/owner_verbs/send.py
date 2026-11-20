"""``/send`` — the owner sends native value or a token to an address.

Why this exists (owner-UX review, 2026-09-26): the most basic wallet action had
no owner verb on any seat. The shape is the ``/bridge`` / ``/pay`` model:

* bare ``/send …`` QUOTES — a guarded dry run: the simulated value, the hard
  caps it must fit (per transaction, daily used/left) and what `go` will do;
* ``/send … go`` SENDS. Typing ``go`` is the owner's own act — the owner typed
  the address — so there is no second tap. It is a genuine owner turn
  (``core.money.authority.owner_direct_turn``): the pause and the autonomous
  ceiling do not bind it; the per-tx and daily caps, the simulation and the
  declared bound do, and they are shown BEFORE the send.

⚠️ 125 S1: the bound comes from the rail's typed facts (``metadata``:
``simulated_usd``, ``sent_usd``, ``unverified``, ``allowed``), never from the
report text — the header prints the token's symbol, text a contract controls.
No text fallback: a quote with no facts refuses. Every tool call runs through
the seat policy chain, so the operator denylist stops an owner ``/send`` too.

Both seats run :func:`send_reply` (Telegram ``surfaces/telegram/send_ops.py``,
the REPL ``cli/ui/commands/h_send.py``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

from core.surfaces.cards import GO_WORDS as _GO
from tools.defi.owner_verbs._seat import (
    caps_line, facts, owner_ctx, owner_words, render, run_tool,
)
from tools.defi.send_order import ANYWAY, TOKEN_COUNT_ROOM, USAGE, parse

logger = logging.getLogger(__name__)

#: The kinds of :class:`SendStep`.
QUOTE, NEEDS_ANYWAY, BOUND, REFUSED, SENT = "quote", "needs_anyway", "bound", "refused", "sent"


@dataclass
class SendStep:
    """One decided step of a ``/send``.

    ``kind``: ``quote`` (a passing dry run; ``bound`` is what a Confirm holds the
    send to, None when the quote carried no value), ``needs_anyway`` (the value is
    NOT VERIFIED and the owner has not typed ``anyway``), ``bound`` (refused: the
    price moved above the confirmed bound), ``refused`` (the quote or the send did
    not pass; ``text`` says why), ``sent`` (the live send ran; ``result`` is its
    report)."""
    kind: str
    text: str = ""
    usd: Optional[float] = None
    bound: Optional[float] = None
    result: Any = None
    quote: Any = None
    figures: dict = field(default_factory=dict)


def bound_usd(meta: Optional[dict]) -> Optional[float]:
    """The value a bound is held to: ``sent_usd`` (no network fee — the number
    the guard holds max_spend_usd to), else ``simulated_usd``. Before 2026-11-13
    the seat compared the FEE-INCLUDED value with a `$` bound made from the
    dollars alone, so `/send $5` of a pinned USDC to a new Solana address refused
    "the price moved" on its rent."""
    if not meta:
        return None
    for key in ("sent_usd", "simulated_usd"):
        value = meta.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def first_send_line(user_id: str, to: str) -> str:
    """A warning when no earlier confirmed send went to this address. Solana
    addresses have no checksum at all, so this is the only typo signal there."""
    try:
        from core.surfaces import cards
        seen = {a.lower() for a in cards.store().recent_recipients(str(user_id))}
    except Exception:
        return ""
    if to.lower() in seen:
        return ""
    return ("  ⚠️ first send to this address from a card — check every "
            "character of it before you confirm\n")


def recent_recipients_block(user_id: str, limit: int = 5) -> str:
    """The addresses earlier confirmed sends went to, in full — copy one
    instead of typing 42 characters. Empty when there are none (or the store
    cannot be read: this is a convenience, never a claim)."""
    try:
        from core.surfaces import cards
        recent = cards.store().recent_recipients(str(user_id))[:limit]
    except Exception:
        return ""
    if not recent:
        return ""
    return ("\n\nRecent recipients (from sends you confirmed):\n"
            + "\n".join(f"  {a}" for a in recent))


def confirm_args(args: List[str], order: dict, bound: float) -> List[str]:
    """The typed words plus the price bound (unless the owner typed one)."""
    words = [w for w in args if w.lower() not in _GO]
    if order.get("max_usd") is not None:
        return words
    return words + ["max", f"{bound:.2f}"]


async def run_send(tool, order: dict, ctx, *, dry_run: bool, max_spend_usd: float):
    """One send-rail call through the seat policy chain. A quote always simulates
    with the acknowledgement (nothing moves), so it can SHOW an unverified value;
    a live send carries only what was typed."""
    anyway = dry_run or bool(order.get("anyway"))
    if order["chain"] == "solana":
        from tools.defi.solana_send_verb import SolanaTransferParams
        params = SolanaTransferParams(token=order["token"], to=order["to"],
                                      amount=order["amount"],
                                      max_spend_usd=max_spend_usd, dry_run=dry_run,
                                      send_anyway=anyway)
        return await run_tool(tool, tool_id="defi_trade", verb="solana_transfer",
                              params=params, ctx=ctx,
                              call=lambda: tool.solana_transfer(params, ctx))
    from tools.defi.trade_tool import TransferParams
    params = TransferParams(chain=order["chain"], token=order["token"], to=order["to"],
                            amount=order["amount"], max_spend_usd=max_spend_usd,
                            dry_run=dry_run, send_anyway=anyway)
    return await run_tool(tool, tool_id="defi_trade", verb="transfer", params=params,
                          ctx=ctx, call=lambda: tool.transfer(params, ctx))


async def decide_send(tool, order: dict, ctx, cap: float) -> SendStep:
    """Quote *order*, then (with ``go``) send it once, bounded by the quote.

    The value the send is held to comes from the quote's typed facts, never
    from a number typed, guessed or parsed out of the report."""
    try:
        quote = await run_send(tool, order, ctx, dry_run=True, max_spend_usd=cap)
    except Exception as exc:
        logger.warning("/send quote failed", exc_info=True)
        return SendStep(REFUSED, text=f"❌ The quote did not run: {exc}. Nothing was sent.",
                        figures={"bare": True})
    meta = facts(quote)
    if getattr(quote, "error", None) or (meta is not None and not meta.get("allowed")):
        return SendStep(REFUSED, text=render(quote), quote=quote)
    if meta is None:
        return SendStep(REFUSED, quote=quote, text=(
            owner_words(render(quote)) + "\n❌ The quote carried no simulated value, so "
            "I cannot bound the send. Nothing was sent."))
    usd = bound_usd(meta)
    unverified = bool(meta.get("unverified"))
    if unverified and not order["anyway"]:
        return SendStep(NEEDS_ANYWAY, usd=usd, quote=quote)
    if not order["go"]:
        bound = None
        if usd is not None:
            bound = (min(cap, order["max_usd"]) if order.get("max_usd") is not None
                     else min(cap, round(usd * TOKEN_COUNT_ROOM + 0.01, 2)))
        return SendStep(QUOTE, usd=usd, bound=bound, quote=quote)
    if usd is None and not unverified:
        return SendStep(REFUSED, figures={"no_value": True}, text=(
            "❌ The quote carried no simulated value, so I cannot bound the "
            "send. Nothing was sent."))
    # An unpriced send (acknowledged above) is held to the per-tx cap; the guard
    # charges it a placeholder that never reads as free.
    bound = cap if usd is None else min(cap, round(usd * TOKEN_COUNT_ROOM + 0.01, 2))
    if order.get("max_usd") is not None:
        # The owner (or the card he confirmed) named the most this send may be
        # worth: the guard refuses a send that simulates above it.
        bound = min(cap, order["max_usd"])
        if usd is not None and usd > bound:
            return SendStep(BOUND, usd=usd, bound=bound, quote=quote)
    try:
        sent = await run_send(tool, order, ctx, dry_run=False, max_spend_usd=bound)
    except Exception as exc:
        logger.warning("/send failed", exc_info=True)
        return SendStep(REFUSED, figures={"bare": True}, text=(
            f"❌ The send raised before a result: {exc}. Check /book before "
            "trying again — assume nothing about what happened."))
    return SendStep(SENT, usd=usd, bound=bound, result=sent, quote=quote)


async def _resolve_usd(tool, order: dict):
    """``(order, conversion line, refusal)`` — a ``$`` amount turned into tokens
    at the send rail's own spend-grade price (``tool._price``), off the loop."""
    import asyncio
    from tools.defi import send_order
    if order.get("usd") is None:
        return order, "", None
    price = getattr(tool, "_price", None)
    try:
        out, px, why = await asyncio.to_thread(send_order.resolve_usd, order, price)
    except Exception as exc:
        logger.warning("/send $ conversion failed", exc_info=True)
        return None, "", (f"❌ The price read for the dollar amount failed ({exc}), so "
                          f"I cannot turn ${order['usd']:,.2f} into tokens. Nothing was "
                          f"sent. Try again, or give a token amount.")
    if why:
        return None, "", "❌ " + why
    return out, send_order.usd_line(out, px), None


async def send_reply(user_id: Optional[str], args: List[str], *, tool: Any = None,
                     seat: str = "telegram") -> str:
    """``/send <amount|$usd> <native|token> to <address> on <chain> [go]``.

    The conversion, the quote and the send are ONE decision: each price is
    read once for all three (``tools.intel.price.one_decision``)."""
    from tools.intel.price import one_decision
    with one_decision():
        return await _send_reply(user_id, args, tool=tool, seat=seat)


async def _send_reply(user_id, args, *, tool, seat) -> str:
    if not user_id:
        return "Only the owner can send from the wallet."
    if not args:
        return USAGE + recent_recipients_block(user_id)
    order, err = parse(args)
    if err:
        return err
    try:
        from tools.defi.trade_tool import DefiTradeTool
    except Exception as exc:                      # pragma: no cover - import guard
        return f"The send rail is unavailable: {exc}"
    tool = tool or DefiTradeTool()
    ctx = owner_ctx(user_id, seat=seat, verb="send")
    try:
        wallet = tool._get_wallet()
    except Exception:
        wallet = None
    if wallet is None:
        return ("❌ The agent wallet is not enabled on this instance, so there is "
                "nothing to send from. Nothing was sent.")
    try:
        cap = float(wallet.policy.per_tx_cap_usd)
    except Exception:
        return ("❌ The wallet is not readable, so I cannot hold this send to a "
                "cap. Nothing was sent.")
    caps = caps_line(tool)
    # A `$` amount: the tool converts it (never the model), and the quote shows
    # the token count and the price. The typed words keep `$…`: a Confirm
    # re-converts at the then price, held to the bound the quote showed.
    typed_order = order
    order, usd_note, why = await _resolve_usd(tool, order)
    if why:
        return why + "\n" + caps
    step = await decide_send(tool, order, ctx, cap)
    return render_send(step, user_id=user_id, args=args, order=order,
                       typed_order=typed_order, usd_note=usd_note, caps=caps, cap=cap)


def render_send(step: SendStep, *, user_id, args, order, typed_order, usd_note, caps,
                cap) -> str:
    """The owner's text for one :class:`SendStep` (every seat shows the same)."""
    typed = [w for w in args if w.lower() not in _GO]
    if step.kind == REFUSED:
        if step.figures.get("bare"):
            return step.text
        if step.figures.get("no_value"):
            return step.text + "\n" + caps
        return usd_note + step.text + "\n" + caps
    body = owner_words(render(step.quote))
    if step.kind == NEEDS_ANYWAY:
        # The extra confirmation (owner, 2026-10-17: "a wallet can send any
        # token … allow with extra confirm"). No `go` line here: the next step
        # is a second quote that carries `anyway`, and only ITS Confirm sends.
        head = ("❌ Nothing was sent. " if order["go"] else "")
        return (head + usd_note + body + "\n" + caps
                + first_send_line(user_id, order["to"])
                + "\n⚠️ EXTRA CHECK: this token's value is NOT VERIFIED. Send it only "
                  "if you know what it is and the amount is right.\n"
                + f"To continue: /send {' '.join(typed + [ANYWAY])}")
    if step.kind == QUOTE:
        if step.bound is None:
            line = (confirm_args(args, typed_order, min(cap, order["max_usd"]))
                    if order.get("usd") is not None else args)
            return (usd_note + body + "\n" + caps
                    + f"\nTo send it: /send {' '.join(line)} go")
        basis = ("the dollars you named + 5%" if order.get("usd") is not None
                 and typed_order.get("max_usd") is None
                 else "your max" if typed_order.get("max_usd") is not None
                 else "the quote + 25%: the token count is fixed, so only a "
                      "large price move stops it")
        return (usd_note + body + "\n" + caps
                + first_send_line(user_id, order["to"])
                + f"  bound: the send may be worth at most ${step.bound:,.2f} ({basis})\n"
                + f"\nTo send it: /send {' '.join(confirm_args(args, typed_order, step.bound))} go")
    if step.kind == BOUND:
        return (usd_note + f"❌ The price moved: the send now simulates at ${step.usd:,.2f}, "
                f"above the ${step.bound:,.2f} you confirmed. Nothing was sent. "
                f"Quote it again: /send {' '.join(typed)}\n" + caps)
    return usd_note + render(step.result) + "\n" + caps


__all__ = ["SendStep", "bound_usd", "decide_send", "render_send", "run_send", "send_reply"]
