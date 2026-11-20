"""``/check`` — look at any address or ticker from an owner seat (071; 125 S12).

The read is the agent's own action, ``defi_data.address_report`` (a ticker:
``defi_data.token_resolve``); this verb parses the words, runs the action
through the seat policy chain and shows the result to the owner. Telegram and
the REPL run :func:`check_reply`, so the seats and the agent cannot answer
differently. Read-only: it never writes a trust store and never moves value.
"""
from __future__ import annotations

import logging
import re
from typing import Any, List, Optional

from tools.defi.data_address import BASE58, EVM_ADDR
from tools.defi.owner_verbs._seat import owner_ctx, render, run_tool

logger = logging.getLogger(__name__)

USAGE = """Usage: /check <address> [chain]
/check <TICKER>
A wallet: what it holds. A token: identity, price, safety screen, holders.
A base58 address is read on solana. A 0x address needs a chain for the tokens it
holds — without one I find where it is a token, or show its native balance on
every EVM chain."""

#: Operator remedies that name an env var. The owner reads /check on a phone;
#: the env-var remedy is for the operator's config, so the owner text drops it
#: (the model-facing tool result keeps it). Pure text rewrites.
_OWNER_REWRITES = (
    (re.compile(r"\s*A keyed RPC pinned in DEFI_SOLANA_RPC makes the per-address read reliable\.?"), ""),
    (re.compile(r" \(or raise DEFI_PORTFOLIO_PRICE_BUDGET_SEC\)"), ""),
    (re.compile(r"no ALCHEMY_API_KEY, so holdings could not be enumerated"),
     "no token index is configured, so holdings could not be listed in full"),
)


def owner_text(text: str) -> str:
    for pattern, repl in _OWNER_REWRITES:
        text = pattern.sub(repl, text)
    return text


def render_check(result: Any) -> str:
    """The owner's text for one ``address_report`` / ``token_resolve`` result."""
    return owner_text(render(result, what="read"))


async def _read(user_id: str, verb: str, params, *, seat: str, tool: Any = None):
    from tools.defi.data_tool import DefiDataTool
    tool = tool or DefiDataTool()
    ctx = owner_ctx(user_id, seat=seat, verb="check")
    return await run_tool(tool, tool_id="defi_data", verb=verb, params=params, ctx=ctx,
                          call=lambda: getattr(tool, verb)(params, ctx), as_owner=True)


async def check_reply(user_id: Optional[str], args: List[str], *, seat: str = "telegram",
                      tool: Any = None) -> str:
    if not user_id:
        return "Only the owner can use /check."
    words = [w for w in (args or []) if w and w.strip()]
    if not words:
        return USAGE
    target = words[0].strip()
    chain = words[1].strip().lower() if len(words) > 1 else None
    if chain is not None:
        from core.wallet import chains
        if chains.get(chain) is None:
            return f"I do not know the chain {chain!r}. Chains: {', '.join(chains.names())}."
    try:
        if EVM_ADDR.match(target) or BASE58.match(target):
            from tools.defi.data_address import AddressReportParams
            return render_check(await _read(
                user_id, "address_report", AddressReportParams(address=target, chain=chain),
                seat=seat, tool=tool))
        from tools.defi.data_tool import ResolveParams
        return ("Not an address — searched it as a ticker.\n"
                + render_check(await _read(user_id, "token_resolve",
                                           ResolveParams(symbol=target), seat=seat, tool=tool)))
    except Exception as exc:
        logger.debug("/check failed", exc_info=True)
        return (f"I could not complete the check ({type(exc).__name__}: {exc}). "
                f"That is UNKNOWN, not a clean result.")


__all__ = ["USAGE", "check_reply", "render_check"]
