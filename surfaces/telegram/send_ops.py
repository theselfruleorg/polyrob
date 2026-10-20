"""``/send`` — the owner sends native value or a token to an address, from chat.

Why this exists (owner-UX review, 2026-09-26): the most basic wallet action had
no owner verb on any seat. The owner asked his agent to send 0.9976 ETH and
could not for three hours. The agent guessed tool spellings, dry-ran six
times, and told him the send was "in your approval queue" when nothing was.

The shape is the ``/bridge`` / ``/pay`` model:

* bare ``/send …`` QUOTES — a guarded dry run: the simulated value, the hard
  caps it must fit (per transaction, daily used/left) and what `go` will do;
* ``/send … go`` SENDS. Typing ``go`` is the owner's own act — the owner typed
  the address — so there is no second tap. It is a genuine owner turn
  (``core.money.authority.owner_direct_turn``): the pause and the autonomous
  ceiling do not bind it; the per-tx and daily caps, the simulation and the
  declared bound do, and they are shown BEFORE the send.

⚠️ REACH, never policy: every gate is inside ``defi_trade.transfer`` /
``solana_transfer`` and ``tx_guard``. This module parses and renders only.
Contributed verb (``core.money_verbs``): Telegram runs it after its owner gate
and refuses it in a room; the REPL twin is ``cli/ui/commands/h_send.py``.
"""
from __future__ import annotations

import logging
import re
from typing import Any, List, Optional

from core.surfaces.cards import GO_WORDS as _GO
from tools.defi.send_order import (  # noqa: F401  (the one parser, re-exported)
    _EVM_ADDR, _NATIVE, _SOL_ADDR, ANYWAY, USAGE, _checksum_ok, _native_on, parse,
)

logger = logging.getLogger(__name__)

#: A quote is re-priced at send time; the declared bound leaves this much room
#: for the price to move between the two (the bridge's own tolerance).
_PRICE_ROOM = 1.05


def _first_send_line(user_id: str, to: str) -> str:
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


def _recent_recipients_block(user_id: str, limit: int = 5) -> str:
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


def _confirm_args(args: List[str], order: dict, bound: float) -> List[str]:
    """The typed words plus the price bound (unless the owner typed one)."""
    words = [w for w in args if w.lower() not in _GO]
    if order.get("max_usd") is not None:
        return words
    return words + ["max", f"{bound:.2f}"]


def _owner_ctx(user_id: str):
    """A genuine owner seat turn (``/bridge`` uses the same shape).

    Each ``/send`` is its own turn: the replay keys hash the turn id, and with
    none the same send to the same address was 'replay blocked' forever on
    Solana (validation, 2026-09-27)."""
    import uuid
    from core.security.turn_facts import owner_seat_context
    return owner_seat_context(user_id, seat="telegram",
                              turn_id=f"send-{uuid.uuid4().hex}")


def _caps_line(tool) -> str:
    """The hard caps this send must fit, BEFORE it runs. Never a comfortable
    zero: an unreadable cap says so."""
    try:
        wallet = tool._get_wallet()
        gate = wallet.policy if wallet is not None else None
    except Exception:
        gate = None
    if gate is None:
        return "  caps:  unavailable (wallet not readable)\n"
    parts = []
    try:
        parts.append(f"per transaction ${float(gate.per_tx_cap_usd):,.2f}")
    except Exception:
        parts.append("per transaction unavailable")
    try:
        from core.wallet.tx_notify import caps_from_gate
        used, limit = caps_from_gate(gate)
        if limit is None:
            parts.append("daily: no cap set")
        else:
            parts.append(f"daily ${used:,.2f} used of ${limit:,.2f} "
                         f"(${max(0.0, limit - used):,.2f} left)")
    except Exception:
        parts.append("daily unavailable")
    line = "  caps:  " + "; ".join(parts) + "\n"
    try:
        from core.signer import MODE_LOCAL, signer_mode
        if signer_mode() != MODE_LOCAL:
            line += ("  signer: a separate signer also holds its own caps and "
                     "may refuse a send these allow\n")
    except Exception:
        pass
    return line


def _simulated_usd(result) -> Optional[float]:
    body = getattr(result, "extracted_content", None) or ""
    # The LAST match: the header prints the token's symbol (on-chain text a
    # contract controls) BEFORE the guard's own value line.
    found = re.findall(r"simulated value:\s*\$([0-9][0-9,]*\.?[0-9]*)", body)
    if not found:
        return None
    try:
        return float(found[-1].replace(",", ""))
    except ValueError:
        return None


async def _run(tool, order: dict, ctx, *, dry_run: bool, max_spend_usd: float):
    # A quote always simulates with the acknowledgement (nothing moves), so it
    # can SHOW an unverified value; a live send carries only what was typed.
    anyway = dry_run or bool(order.get("anyway"))
    if order["chain"] == "solana":
        from tools.defi.solana_send_verb import SolanaTransferParams
        params = SolanaTransferParams(token=order["token"], to=order["to"],
                                      amount=order["amount"],
                                      max_spend_usd=max_spend_usd, dry_run=dry_run,
                                      send_anyway=anyway)
        return await tool.solana_transfer(params, ctx)
    from tools.defi.trade_tool import TransferParams
    params = TransferParams(chain=order["chain"], token=order["token"], to=order["to"],
                            amount=order["amount"], max_spend_usd=max_spend_usd,
                            dry_run=dry_run, send_anyway=anyway)
    return await tool.transfer(params, ctx)


#: The rail's sentence for the AGENT ("call me again live"). The owner's way to
#: send is the `go` line this seat appends, so the sentence is dropped here.
_AGENT_RERUN = re.compile(r"\s*Re-run with dry_run=false to [a-z]+\.")


def _owner_words(text: str) -> str:
    return _AGENT_RERUN.sub("", text or "")


def _render(result) -> str:
    if getattr(result, "error", None):
        return f"❌ {result.error}"
    body = getattr(result, "extracted_content", None)
    if not body:
        return ("The send returned neither an error nor a report. That is a bug — "
                "do NOT retry until it is understood; assume nothing about what "
                "happened to the funds.")
    return body


async def send_reply(user_id: Optional[str], args: List[str], *, tool: Any = None) -> str:
    """``/send <amount> <native|token> to <address> on <chain> [go]``."""
    if not user_id:
        return "Only the owner can send from the wallet."
    if not args:
        return USAGE + _recent_recipients_block(user_id)
    order, err = parse(args)
    if err:
        return err
    try:
        from tools.defi.trade_tool import DefiTradeTool
    except Exception as exc:                      # pragma: no cover - import guard
        return f"The send rail is unavailable: {exc}"
    tool = tool or DefiTradeTool()
    ctx = _owner_ctx(user_id)
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
    caps = _caps_line(tool)

    # Always quote first: the value the send will be held to comes from the
    # simulation, never from a number typed or guessed.
    try:
        quote = await _run(tool, order, ctx, dry_run=True, max_spend_usd=cap)
    except Exception as exc:
        logger.warning("/send quote failed", exc_info=True)
        return f"❌ The quote did not run: {exc}. Nothing was sent."
    if getattr(quote, "error", None) or "DRY RUN" not in (quote.extracted_content or ""):
        return _render(quote) + "\n" + caps
    usd = _simulated_usd(quote)
    from core.wallet.tx_guard_steps.value import UNVERIFIED_MARK
    unverified = UNVERIFIED_MARK in (quote.extracted_content or "")
    typed = [w for w in args if w.lower() not in _GO]
    if unverified and not order["anyway"]:
        # The extra confirmation (owner, 2026-10-17: "a wallet can send any
        # token … allow with extra confirm"). No `go` line here: the next step
        # is a second quote that carries `anyway`, and only ITS Confirm sends.
        head = ("❌ Nothing was sent. " if order["go"] else "")
        return (head + _owner_words(_render(quote)) + "\n" + caps
                + _first_send_line(user_id, order["to"])
                + "\n⚠️ EXTRA CHECK: this token's value is NOT VERIFIED. Send it only "
                  "if you know what it is and the amount is right.\n"
                + f"To continue: /send {' '.join(typed + [ANYWAY])}")
    if not order["go"]:
        if usd is None:
            return (_owner_words(_render(quote)) + "\n" + caps
                    + f"\nTo send it: /send {' '.join(args)} go")
        bound = (min(cap, order["max_usd"]) if order.get("max_usd") is not None
                 else min(cap, round(usd * _PRICE_ROOM + 0.01, 2)))
        return (_owner_words(_render(quote)) + "\n" + caps
                + _first_send_line(user_id, order["to"])
                + f"  bound: the send may be worth at most ${bound:,.2f} "
                  f"(the quote + 5% for the price to move)\n"
                + f"\nTo send it: /send {' '.join(_confirm_args(args, order, bound))} go")

    if usd is None and not unverified:
        return ("❌ The quote carried no simulated value, so I cannot bound the "
                "send. Nothing was sent.\n" + caps)
    # An unpriced send (acknowledged above) is held to the per-tx cap; the guard
    # charges it a placeholder that never reads as free.
    bound = cap if usd is None else min(cap, round(usd * _PRICE_ROOM + 0.01, 2))
    if order.get("max_usd") is not None:
        # The owner (or the card he confirmed) named the most this send may
        # be worth: the guard refuses a send that simulates above it.
        bound = min(cap, order["max_usd"])
        if usd is not None and usd > bound:
            return (f"❌ The price moved: the send now simulates at ${usd:,.2f}, "
                    f"above the ${bound:,.2f} you confirmed. Nothing was sent. "
                    f"Quote it again: /send "
                    f"{' '.join(w for w in args if w.lower() not in _GO)}\n" + caps)
    try:
        sent = await _run(tool, order, ctx, dry_run=False, max_spend_usd=bound)
    except Exception as exc:
        logger.warning("/send failed", exc_info=True)
        return (f"❌ The send raised before a result: {exc}. Check /book before "
                "trying again — assume nothing about what happened.")
    return _render(sent) + "\n" + caps


async def send_verb(*, user_id: str, data_dir: str, args: List[str],
                    task_agent: Any = None, result: Any = None,
                    board: Any = None) -> str:
    """Contributed-verb entry (``core.money_verbs``)."""
    return await send_reply(user_id, list(args or []))
