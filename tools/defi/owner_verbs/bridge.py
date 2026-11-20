"""``/bridge`` — move value across chains from an owner seat (037).

The body every seat runs (proposal 125 S14): the Telegram seat table
(``surfaces/telegram/admin_verbs.py``) and the REPL import it from here. The rail call runs through the seat policy
chain (the operator denylist and the effect gate), with the one owner-seat context.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from core.surfaces.cards import is_go_word

logger = logging.getLogger(__name__)


def bridge_chain_names() -> str:
    """The destination chains this build actually knows, from the ONE registry.

    ⚠️ C62: this was a hand-typed list ("solana, base, robinhood, ethereum,
    arbitrum, polygon") that could only ever drift from
    ``core.wallet.chains``. Fail-open to naming the remedy rather than a stale
    list — a wrong list of money chains is worse than no list.
    """
    try:
        from core.wallet import chains as _chains
        names = sorted(set(_chains.names()) | {"solana"})
        return ", ".join(names)
    except Exception:
        logger.warning("chain registry unreadable for /bridge usage",
                       exc_info=True)
        return "(I could not read my chain registry — try `/book`)"


async def bridge_reply(user_id: Optional[str], data_dir: str,
                       args: List[str], *, seat: str = "telegram") -> str:
    """`/bridge <from> <to> <amount> [from usdc] [as <asset>] [go]` — move value across chains.

    Why this exists: the bridge shipped CLI-only, which meant the one person
    allowed to run it had to SSH to the box. The owner's standing directive is
    that he can do anything from the chat, and he is usually on a phone. Without
    this verb the whole rail is unreachable from where he actually is.

    Bare form QUOTES (dry run — asserts everything, broadcasts nothing). Adding
    `go` executes: within the autonomous ceiling it runs and reports; above it,
    the durable owner-approval queue holds it — typing `go` is a deliberate
    second act, not the approval itself.

    ⚠️ D12: ``async`` and awaited. This used to call ``asyncio.run`` on the
    polling loop's own thread, catch the resulting ``RuntimeError`` and hand the
    coroutine to a thread pool, then BLOCK on ``.result()`` — so every inbound
    Telegram update stalled for the whole quote (a bridge quote is seconds of
    network), and any loop-affine object the verb touched belonged to the wrong
    loop. The rail underneath is async; this seat is async.

    ⚠️ C63: ``as <asset>`` names the DESTINATION asset (``BridgeParams.
    token_out``) — ``native`` by default, or a token PINNED in the destination
    chain's registry row. An arbitrary address is refused by the rail, not here.
    """
    if not user_id:
        return "Only the owner can bridge."
    if len(args) < 3:
        return ("Usage: /bridge <from> <to> <amount> [from usdc] [as <asset>] [go]\n"
                "e.g. /bridge solana robinhood 0.9          — quote only\n"
                "     /bridge solana robinhood 0.9 go       — execute\n"
                "     /bridge robinhood solana 0.1 as usdc go  — ETH in, USDC on Solana\n"
                "     /bridge base solana 50 from usdc as usdc — USDC to USDC\n\n"
                f"Chains: {bridge_chain_names()}.\n"
                "The origin is NATIVE (SOL/ETH), or `from usdc` for the chain's "
                "pinned USDC. `as` picks what ARRIVES: native (default), or a "
                "token pinned for that chain.\n"
                "Under your autonomous ceiling it runs and reports; above it, "
                "it waits for you in /pending.")

    tokens = list(args)
    execute = False
    if tokens and is_go_word(tokens[-1]):
        execute = True
        tokens.pop()

    token_out = "native"
    for i, word in enumerate(tokens):
        if word.lower() in ("as", "into", "to_asset") and i + 1 < len(tokens):
            token_out = tokens[i + 1]
            tokens = tokens[:i] + tokens[i + 2:]
            break
    token_in = "native"
    for i, word in enumerate(tokens):
        if word.lower() in ("from", "with") and i + 1 < len(tokens):
            token_in = tokens[i + 1]
            tokens = tokens[:i] + tokens[i + 2:]
            break

    if len(tokens) < 3:
        return "Usage: /bridge <from> <to> <amount> [from usdc] [as <asset>] [go]"
    from_chain, to_chain, amount_raw = tokens[0], tokens[1], tokens[2]
    try:
        amount = float(amount_raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        return f"Amount must be a positive number, got {amount_raw!r}."

    try:
        from tools.defi.bridge_verb import perform_bridge
        from tools.defi.trade_tool import BridgeParams, DefiTradeTool
    except Exception as exc:                       # pragma: no cover - import guard
        return f"The bridge rail is unavailable: {exc}"

    params = BridgeParams(from_chain=from_chain, to_chain=to_chain,
                          amount=amount, token_in=token_in, token_out=token_out,
                          dry_run=not execute)
    # A genuine owner chat turn. Not forged, not a sub-agent — the same seat the
    # CLI is, reached from the phone instead of a shell.
    from tools.defi.owner_verbs._seat import owner_ctx, run_tool
    ctx = owner_ctx(user_id, seat=seat, verb="bridge")
    tool = DefiTradeTool()
    try:
        result = await run_tool(tool, tool_id="defi_trade", verb="bridge", params=params,
                                ctx=ctx, call=lambda: perform_bridge(tool, params, ctx))
    except Exception as exc:
        logger.warning("bridge verb failed", exc_info=True)
        return f"The bridge did not run: {exc}"

    if getattr(result, "error", None):
        return f"❌ {result.error}"
    body = getattr(result, "extracted_content", None)
    if not body:
        return ("The bridge returned neither an error nor a report. That is a "
                "bug — do NOT retry until it is understood; assume nothing "
                "about what happened to the funds.")
    if not execute:
        _as = ((f" from {token_in}" if token_in != "native" else "")
               + (f" as {token_out}" if token_out != "native" else ""))
        from core.surfaces.cards import with_go_hint
        body = with_go_hint(body, "\n\nAdd `go` to execute: "
                            f"/bridge {from_chain} {to_chain} {amount_raw}{_as} go")
    return body



__all__ = ["bridge_chain_names", "bridge_reply"]
