"""`/claim` — collect the creator fees a launchpad already owes this agent.

E6: ``launchpad_claim`` shipped as an AGENT action and nothing else, while the
status snapshot told the owner to "run launchpad_claim" — which is not a thing a
human can run from anywhere. 13.6 ETH of the agent's own revenue sat in a Pons
fee escrow for exactly that reason (2026-09-14). This is the owner's seat.

⚠️ REACH, not policy. The verb underneath is unchanged: ``tx_guard`` still
adjudicates, the claim is still the third intent shape (``is_claim`` — sends
nothing, must DECLARE what it expects to receive, and any outflow refuses), and
the caps and the durable owner queue still apply. Nothing here widens anything.

⚠️ The escrow credits an ADDRESS, not a token: one claim collects what every
token this wallet launched has earned. The caller supplies only a token so the
escrow address is always DERIVED — there is no parameter that can aim it
elsewhere.

Shared: the REPL and the CLI import :func:`claim_reply` rather than re-deriving
the parse or the sentences.
"""
from __future__ import annotations

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

#: The most USD of FEE a chat-launched claim authorises. A claim SENDS nothing,
#: so this bounds gas only — but it is priced at the worst case, never $0.00.
_MAX_FEE_USD = 2.0

USAGE = (
    "Usage: /claim <token-address> [go]\n"
    "e.g. /claim 0xabc…            — read what is owed, broadcast nothing\n"
    "     /claim 0xabc… go         — actually claim it\n\n"
    "`token` is a contract I launched. The curve, the fee escrow and the "
    "amount are all read on-chain from it — there is no parameter that can "
    "point this at another contract.\n"
    "⚠️ The escrow credits an ADDRESS, so one claim collects what every token "
    "I launched has earned, not just this one."
)


async def claim_reply(user_id: Optional[str], args: List[str]) -> str:
    """``/claim <token> [go]`` — the owner's launchpad fee claim.

    Bare form READS (dry run: asserts and reports, broadcasts nothing); `go`
    executes. Returns one chat-ready string; never raises.
    """
    if not user_id:
        return "Only the owner can claim fees."
    tokens = [a for a in (args or []) if str(a).strip()]
    if not tokens:
        return USAGE

    execute = False
    if tokens[-1].lower() in ("go", "execute", "confirm"):
        execute = True
        tokens.pop()
    if not tokens:
        return USAGE
    token = tokens[0]

    try:
        from tools.launchpad.tool import ClaimParams, LaunchpadTool
    except Exception as exc:                       # pragma: no cover - import guard
        return f"The launchpad rail is unavailable: {exc}"

    from surfaces.telegram.token_ops import _owner_ctx

    params = ClaimParams(token=token, max_spend_usd=_MAX_FEE_USD,
                         dry_run=not execute)
    try:
        result = await LaunchpadTool().launchpad_claim(params, _owner_ctx(user_id))
    except Exception as exc:
        logger.warning("launchpad claim failed", exc_info=True)
        return f"The claim did not run: {exc}"

    if getattr(result, "error", None):
        return f"❌ {result.error}"
    body = getattr(result, "extracted_content", None)
    if not body:
        return ("The claim returned neither an error nor a report. That is a "
                "bug — do NOT retry until it is understood; assume nothing "
                "about where the fees are.")
    if not execute:
        body += f"\n\nAdd `go` to claim it: /claim {token} go"
    return body


__all__ = ["USAGE", "claim_reply"]
