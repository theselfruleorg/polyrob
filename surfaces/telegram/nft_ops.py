"""`/nft` — the owner's seat over the non-fungible plane (E7).

``nft_holdings`` / ``nft_info`` / ``nft_transfer`` / ``nft_revoke_approval``
shipped as AGENT actions and reached no human seat at all, so the owner could
not see what his own treasury held, could not send one, and — the one that
matters — could not RETIRE a standing ``ApprovalForAll`` he had discovered.

⚠️ REACH, not policy. Every gate underneath is untouched:

* ``defi_trade_nft_transfer`` is priced at its worst-case FEE (an NFT is
  unpriceable, so the caps cannot bound it) and stays ALWAYS owner-approved —
  this seat cannot and does not exempt it;
* ``nft_revoke_approval`` is risk-reducing and rides the capped lane;
* the guard's three NFT refusals (an undeclared NFT outflow, a declared move
  the simulation does not emit, and ANY ``ApprovalForAll(operator, true)``) run
  on every transaction whatever this file does;
* there is deliberately no GRANT verb here, because there is none underneath.

⚠️ Enumeration needs an indexer. Without ``ALCHEMY_API_KEY`` the read SAYS SO
rather than printing an empty list — "you own nothing" and "I could not look"
are different facts, and the rail already answers honestly; this seat only
forwards its words.

Shared: the REPL and the CLI import :func:`nft_reply`.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

#: Chain used when the owner names none. Matches the tool defaults.
_DEFAULT_CHAIN = "base"

USAGE = (
    "Usage: /nft — the agent NFTs I own (pinned collections), their accounts and holdings\n"
    "/nft send <id|collection#id> <to> [on <chain>] [go] — give one away (its account goes "
    "with it; its approvals must be cleared first)\n"
    "/nft list [on <chain>] [address <0x…>]\n"
    "/nft info <contract> <token-id> [on <chain>]\n"
    "/nft transfer <contract> <token-id> <to> [on <chain>] "
    "[std erc721|erc1155] [amount <n>] [go]\n"
    "/nft revoke <contract> <operator> [on <chain>] [go]\n\n"
    "Default chain is base. Writes simulate unless you add `go`.\n"
    "⚠️ A transfer is irreversible and a collectible has no reliable price, so "
    "it ALWAYS comes to you for approval — the spend caps cannot bound it.\n"
    "⚠️ `revoke` retires a standing approval over a whole collection. There is "
    "no verb here that GRANTS one, by design."
)


def _pull(tokens: List[str], key: str) -> Optional[str]:
    """Remove ``<key> <value>`` from ``tokens`` and return the value, or None."""
    for i, word in enumerate(tokens):
        if str(word).lower() == key and i + 1 < len(tokens):
            value = tokens[i + 1]
            del tokens[i:i + 2]
            return value
    return None


def _render(result: Any) -> str:
    if getattr(result, "error", None):
        return f"❌ {result.error}"
    body = getattr(result, "extracted_content", None)
    if not body:
        return ("The verb returned neither an error nor a report. That is a "
                "bug — do NOT retry until it is understood; assume nothing "
                "about what happened.")
    return str(body)


async def nft_reply(user_id: Optional[str], args: List[str]) -> str:
    """``/nft list|info|transfer|revoke`` — one chat-ready string. Never raises."""
    if not user_id:
        return "Only the owner can use /nft."
    tokens = [str(a) for a in (args or []) if str(a).strip()]
    if not tokens:
        return await agent_nft_overview()

    verb = tokens.pop(0).lower()
    if verb in ("ls", "holdings"):
        verb = "list"
    if verb not in ("list", "info", "transfer", "revoke", "send"):
        return f"Unknown /nft verb {verb!r}.\n{USAGE}"

    execute = False
    if tokens and tokens[-1].lower() in ("go", "execute", "confirm"):
        execute = True
        tokens.pop()

    named_chain = _pull(tokens, "on")
    chain = named_chain or _DEFAULT_CHAIN
    from surfaces.telegram.token_ops import _owner_ctx
    ctx = _owner_ctx(user_id)

    try:
        from tools.defi.data_tool import (DefiDataTool, NftHoldingsParams,
                                          NftInfoParams)
        from tools.defi.trade_tool import (DefiTradeTool, NftRevokeParams,
                                           NftTransferParams)
    except Exception as exc:                       # pragma: no cover - import guard
        return f"The NFT rail is unavailable: {exc}"

    try:
        if verb == "list":
            address = _pull(tokens, "address") or (tokens[0] if tokens else None)
            result = await DefiDataTool().nft_holdings(
                NftHoldingsParams(chain=chain, address=address), ctx)
            return _render(result)

        if verb == "send":
            if len(tokens) < 2:
                return "Usage: /nft send <id|collection#id> <to> [on <chain>] [go]"
            return await _agent_nft_send(ctx, tokens[0], tokens[1],
                                         chain=named_chain or _AGENT_NFT_CHAIN, execute=execute)

        if verb == "info":
            if len(tokens) < 2:
                return "Usage: /nft info <contract> <token-id> [on <chain>]"
            try:
                token_id = int(tokens[1])
            except (TypeError, ValueError):
                return f"The token id must be a whole number, got {tokens[1]!r}."
            result = await DefiDataTool().nft_info(
                NftInfoParams(chain=chain, contract=tokens[0], token_id=token_id),
                ctx)
            return _render(result)

        if verb == "transfer":
            standard = (_pull(tokens, "std") or _pull(tokens, "standard")
                        or "erc721")
            amount_raw = _pull(tokens, "amount") or "1"
            if len(tokens) < 3:
                return ("Usage: /nft transfer <contract> <token-id> <to> "
                        "[on <chain>] [std erc721|erc1155] [amount <n>] [go]")
            try:
                token_id = int(tokens[1])
                amount = int(amount_raw)
            except (TypeError, ValueError):
                return "The token id and amount must both be whole numbers."
            result = await DefiTradeTool().nft_transfer(
                NftTransferParams(chain=chain, contract=tokens[0],
                                  token_id=token_id, to=tokens[2],
                                  standard=standard.lower(), amount=amount,
                                  dry_run=not execute), ctx)
            body = _render(result)
            if not execute and not body.startswith("❌"):
                body += ("\n\nAdd `go` to send it: /nft transfer "
                         f"{tokens[0]} {token_id} {tokens[2]} on {chain} go\n"
                         "⚠️ It is irreversible, and it will come to you for "
                         "approval whatever your caps say.")
            return body

        if len(tokens) < 2:
            return "Usage: /nft revoke <contract> <operator> [on <chain>] [go]"
        result = await DefiTradeTool().nft_revoke_approval(
            NftRevokeParams(chain=chain, contract=tokens[0],
                            operator=tokens[1], dry_run=not execute), ctx)
        body = _render(result)
        if not execute and not body.startswith("❌"):
            body += ("\n\nAdd `go` to retire it: /nft revoke "
                     f"{tokens[0]} {tokens[1]} on {chain} go")
        return body
    except Exception as exc:
        logger.warning("nft verb %s failed", verb, exc_info=True)
        return f"The NFT verb did not run: {exc}"


#: The chain of the agent-NFT verbs when the owner names none (the collection's chain).
_AGENT_NFT_CHAIN = "robinhood"


async def agent_nft_overview() -> str:
    """``/nft`` with no words (069 v4 A4): the NFTs of pinned collections the treasury owns,
    each with its account, the account's native balance, its open approvals and its book rows.
    Reads only; an arrival or a loss found here is reported here."""
    from core.env import bool_env
    if not bool_env("AGENT_NFT_ENABLED", False):
        return "Agent NFTs are off (AGENT_NFT_ENABLED).\n\n" + USAGE
    try:
        import asyncio

        from core.wallet import nft_holdings
        from core.wallet.factory import get_agent_wallet
        wallet = get_agent_wallet()
        if wallet is None:
            return "No agent wallet is configured, so I own no NFT.\n\n" + USAGE
        treasury = wallet.operational_signer().address
        text = await asyncio.to_thread(nft_holdings.overview, treasury=treasury)
    except Exception as exc:
        logger.warning("/nft overview failed", exc_info=True)
        return f"The agent-NFT view did not run: {exc}"
    return text + "\n\n" + USAGE


async def _agent_nft_send(ctx, target: str, to: str, *, chain: str, execute: bool) -> str:
    """``/nft send <id> <to> [go]`` — ``agent_nft_withdraw_token`` from the owner's seat."""
    from tools.agent_nft.tool import AgentNftTool, TakeParams
    try:
        params = TakeParams(to=to, nft=target, chain=chain, dry_run=not execute)
    except Exception as exc:
        return f"❌ {exc}"
    result = await AgentNftTool().agent_nft_withdraw_token(params, ctx)
    body = _render(result)
    if not execute and not body.startswith("❌"):
        body += (f"\n\nAdd `go` to send it: /nft send {target} {to} on {chain} go\n"
                 "⚠️ It is irreversible: the NFT and its account leave this treasury.")
    return body


__all__ = ["USAGE", "agent_nft_overview", "nft_reply"]
