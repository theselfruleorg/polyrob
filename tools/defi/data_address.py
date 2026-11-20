"""``defi_data.address_report`` — look at ANY address (proposal 125 S12).

Mixed into :class:`tools.defi.data_tool.DefiDataTool`. Before 125 this read was the
owner's ``/check`` body inside the Telegram seat, so the agent could not reach it;
now the owner verb and the agent run the SAME action.

What it does with the address:

* a base58 address → read on solana: a wallet's holdings, or a mint's report;
* a 0x address + chain → the same on that chain;
* a 0x address alone → find the EVM chains where it is a TOKEN (code + decimals)
  and report those; if it is a wallet everywhere, show its native balance on
  every EVM chain and ask for a chain for the token list (one address holds
  different things on each chain, and a full read of every chain is slow).

Typed metadata (a seat binds to these, never to the text): ``kind_by_chain``
(``{chain: kind}``, None = the read failed), ``token_on`` (the chains where it is
a token), ``native_by_chain`` (``{chain: float}``, None = UNKNOWN, never 0) and
``text``. Read-only: it never writes a trust store and never moves value.
"""
from __future__ import annotations  # safe: @BaseTool.action uses explicit param_model

import asyncio
import re
from typing import Optional

from pydantic import BaseModel, Field

from tools.base_tool import BaseTool

EVM_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
#: How many token chains a bare 0x address reports in full.
TOKEN_CHAINS_SHOWN = 3


class AddressReportParams(BaseModel):
    address: str = Field(description="Any address: a 0x address (EVM) or a base58 address (solana)")
    chain: Optional[str] = Field(
        default=None,
        description="The chain to read on. Omit for a base58 address (solana); omit for "
                    "a 0x address to find where it is a token, or its native balance "
                    "on every EVM chain")


def _evm_chains():
    from core.wallet import chains
    return [r.name for r in chains.all_rows() if r.family == "evm"]


def _text(result) -> str:
    if getattr(result, "error", None):
        return f"❌ {result.error}"
    body = getattr(result, "extracted_content", None)
    if not body:
        return ("The read returned neither an error nor a report. That is a bug "
                "— do NOT retry until it is understood.")
    return body


class AddressReportMixin:
    """The one "what is this address" read the agent and every owner seat run."""

    @BaseTool.action(
        "Look at ANY address: a wallet's holdings or a token's report (identity, "
        "price, safety screen, holders). A base58 address is read on solana. A 0x "
        "address with a chain is read there; without one, the chains where it is a "
        "token are found and reported, else its native balance on every EVM chain. "
        "A failed read is UNKNOWN, never zero. Read-only.",
        param_model=AddressReportParams)
    async def address_report(self, params: AddressReportParams, execution_context=None):
        address = (params.address or "").strip()
        chain = (params.chain or "").strip().lower() or None
        if chain is not None:
            from core.wallet import chains
            if chains.get(chain) is None:
                return self._ar(error=(f"I do not know the chain {chain!r}. Chains: "
                                       f"{', '.join(chains.names())}."))
        meta = {"kind_by_chain": {}, "token_on": [], "native_by_chain": {}}
        if EVM_ADDR.match(address):
            text = (await self._address_everywhere(address, meta, execution_context)
                    if chain is None else
                    await self._address_on(address, chain, meta, execution_context))
        elif BASE58.match(address):
            text = await self._address_on(address, chain or "solana", meta, execution_context)
        else:
            return self._ar(error=(f"{address!r} is not an address. For a ticker, use "
                                   f"token_resolve."))
        meta["text"] = text
        return self._ar(content=text, metadata=meta)

    async def _address_on(self, address: str, chain: str, meta: dict, ctx) -> str:
        """A wallet's holdings or a token's report on ONE chain."""
        from core.wallet.address_kind import classify
        from tools.defi.data_params import TokenRefParams, WalletHoldingsParams
        try:
            kind = await asyncio.to_thread(classify, chain, address)
        except Exception:
            kind = None
        k = (kind or {}).get("kind") if isinstance(kind, dict) else None
        meta["kind_by_chain"][chain] = k
        if k == "token":
            if chain not in meta["token_on"]:
                meta["token_on"].append(chain)
            return _text(await self.token_info(TokenRefParams(chain=chain, address=address),
                                               ctx))
        if k == "token_account":
            mint = kind.get("mint") or "?"
            owner = kind.get("owner") or "?"
            return (f"{address} on {chain} is a token ACCOUNT (one balance slot).\n"
                    f"  mint:  {mint}   → /check {mint}\n"
                    f"  owner: {owner}   → /check {owner}")
        # A wallet, a contract (a Safe holds tokens too), or unknown: the holdings
        # read refuses by itself if the address turns out to be something else.
        return _text(await self.wallet_holdings(
            WalletHoldingsParams(address=address, chain=chain), ctx))

    async def _address_everywhere(self, address: str, meta: dict, ctx) -> str:
        """A bare 0x address: where is it a token, else native balance per chain."""
        from core.wallet import chains
        from core.wallet.address_kind import classify
        from core.wallet.onchain import balances
        names = _evm_chains()
        kinds = await asyncio.gather(*(asyncio.to_thread(classify, c, address) for c in names),
                                     return_exceptions=True)
        for c, k in zip(names, kinds):
            meta["kind_by_chain"][c] = k.get("kind") if isinstance(k, dict) else None
        token_on = [c for c in names if meta["kind_by_chain"][c] == "token"]
        meta["token_on"] = list(token_on)
        if token_on:
            parts = []
            for c in token_on[:TOKEN_CHAINS_SHOWN]:
                parts.append(await self._address_on(address, c, meta, ctx))
            if len(token_on) > TOKEN_CHAINS_SHOWN:
                parts.append(f"(also a token on: {', '.join(token_on[TOKEN_CHAINS_SHOWN:])} — "
                             f"/check {address} <chain>)")
            return "\n\n".join(parts)
        natives = await asyncio.gather(*(asyncio.to_thread(balances, address, c) for c in names),
                                       return_exceptions=True)
        lines = [f"{address} — native balance on each EVM chain:"]
        for c, got in zip(names, natives):
            native = got[0] if isinstance(got, tuple) else None
            if native is not None:
                try:
                    native = float(native)
                except (TypeError, ValueError):
                    native = None
            meta["native_by_chain"][c] = native
            row = chains.get(c)
            sym = row.native_symbol if row else "native"
            shown = "UNKNOWN (read failed — not zero)" if native is None else f"{native:,.6f} {sym}"
            lines.append(f"  {c:<18} {shown}")
        lines += ["", f"Tokens differ per chain. For the full list: /check {address} <chain>"]
        return "\n".join(lines)


__all__ = ["AddressReportMixin", "AddressReportParams"]
