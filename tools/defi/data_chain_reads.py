"""``defi_data`` direct chain reads — NFT holdings and info, and ``contract_read``.

The sync bodies behind ``nft_holdings``, ``nft_info`` and ``contract_read``,
plus the bounded ``eth_call`` and its best-effort decode. Split out of
``data_tool.py`` (W9, behaviour-preserving) as a mixin, so every
``DefiDataTool._*`` name resolves as before. Module-level names of
``data_tool`` are imported at call time.
"""
from __future__ import annotations

import logging

from typing import List, Optional
from tools.defi.data_params import ContractReadParams, NftHoldingsParams, NftInfoParams
from tools.defi.token_screen import quoted

logger = logging.getLogger("tools.defi.data_tool")


class ChainReadsMixin:
    """Mixed into :class:`tools.defi.data_tool.DefiDataTool`."""

    def _nft_holdings_sync(self, params: NftHoldingsParams, execution_context=None):
        from tools.defi.data_tool import _operator_read_refusal  # call-time
        from tools.defi import nft_verbs

        evm_err = self._evm_only(params.chain, "nft_holdings")
        if evm_err:
            return self._ar(error=evm_err)
        address = params.address
        if not address:
            refusal = _operator_read_refusal(execution_context)
            if refusal:
                return self._ar(error=refusal)
            try:
                from core.wallet.factory import get_agent_wallet
                w = get_agent_wallet()
                address = w.operational_signer().address if w else None
            except Exception:
                address = None
        if not address:
            return self._ar(error=(
                "no address given and this agent's own wallet could not be "
                "resolved (AGENT_WALLET_ENABLED)"))
        try:
            held = nft_verbs.enumerate_holdings(
                chain=params.chain, address=address, fetch=self._nft_fetch)
        except nft_verbs.EnumerationUnavailable as exc:
            # UNAVAILABLE is not EMPTY. Rendering [] here would read as a
            # confident "you hold nothing".
            return self._ar(error=str(exc))
        except Exception as exc:
            return self._ar(error=f"the holdings read failed: {exc}")
        if not held:
            return self._ar(content=(
                f"{address} on {params.chain}: the indexer returned no NFTs. "
                f"This IS an answer from a working read, not a failed one."))
        lines = [f"{address} on {params.chain} — {len(held)} NFT(s):"]
        for item in held[:50]:
            name = quoted(item.get("name"), name=True) or "(unnamed)"
            std = item.get("standard") or "standard unknown"
            bal = f" x{item['balance']}" if item.get("balance") not in (None, "1") else ""
            lines.append(f"  {name} — {item['contract']} #{item.get('token_id')} "
                         f"[{std}]{bal}")
        if len(held) > 50:
            lines.append(f"  …and {len(held) - 50} more")
        return self._ar(content="\n".join(lines))

    def _nft_info_sync(self, params: NftInfoParams, execution_context=None):
        from core.wallet.onchain import _rpc, rpc_url_for_chain
        from tools.defi import nft_verbs

        evm_err = self._evm_only(params.chain, "nft_info")
        if evm_err:
            return self._ar(error=evm_err)
        contract, err = self._validate(params.chain, params.contract)
        if err:
            return self._ar(error=err)

        def _call(method, args, timeout=8.0):
            return _rpc(rpc_url_for_chain(params.chain), method, args, timeout)

        try:
            facts = nft_verbs.read_token_facts(
                _call, chain=params.chain, contract=contract,
                token_id=params.token_id)
        except Exception as exc:
            return self._ar(error=f"the token read failed: {exc}")
        lines = [f"{contract} #{params.token_id} on {params.chain}"]
        if facts.get("standard"):
            lines.append(f"  standard: {facts['standard']}")
        if facts.get("owner"):
            lines.append(f"  owner: {facts['owner']}")
        if facts.get("uri"):
            lines.append(f"  metadata (provider text): {quoted(str(facts['uri'])[:2048], name=True)}")
        if facts.get("not_checked"):
            # ⚠️ A check that did not run is not a check that passed.
            lines.append("  NOT CHECKED: " + "; ".join(facts["not_checked"]))
        return self._ar(content="\n".join(lines))

    def _contract_read_sync(self, params: ContractReadParams, execution_context=None):
        from tools.defi.data_tool import CONTRACT_READ_MAX_BYTES  # call-time
        evm_err = self._evm_only(params.chain, "contract_read")
        if evm_err:
            return self._ar(error=evm_err)
        address, err = self._validate(params.chain, params.address)
        if err:
            return self._ar(error=err)
        try:
            raw = self._eth_call(chain=params.chain, address=address,
                                 signature=params.signature, args=params.args)
        except Exception as exc:
            return self._ar(error=f"contract_read failed: {exc}")
        if raw is None:
            return self._ar(content=f"{address}.{params.signature}: no result (call reverted or failed)")

        truncated = False
        if len(raw) > 2 + CONTRACT_READ_MAX_BYTES * 2:
            raw = raw[:2 + CONTRACT_READ_MAX_BYTES * 2]
            truncated = True

        lines = [f"{address}.{params.signature}",
                 f"  raw: {raw}"]
        if truncated:
            lines.append(f"  truncated: true (capped at {CONTRACT_READ_MAX_BYTES} bytes)")
        decoded = self._best_effort_decode(raw)
        if decoded is not None:
            lines.append(f"  decoded (best-effort, UNVERIFIED — the interpretation may be "
                         f"wrong if the signature does not match the contract): {decoded}")
        return self._ar(content="\n".join(lines))

    # -- helpers ----------------------------------------------------------
    def _scan_set(self, chain: str) -> List[str]:
        """Canonical list ∪ every address already in the token cache.

        A superset is harmless: scanning an address never held costs one
        multicall slot and returns zero.
        """
        from core.wallet.tokens import CANONICAL_TOKENS
        out = {addr for (c, addr) in CANONICAL_TOKENS if c == chain}
        try:
            from core import sqlite_util
            from core.wallet.tokens import _db, _ensure_schema
            path = _db(None)
            _ensure_schema(path)
            conn = sqlite_util.wal_connect(path)
            try:
                for (addr,) in conn.execute(
                        "SELECT address FROM tokens WHERE chain = ?", (chain,)):
                    out.add(addr)
            finally:
                conn.close()
        except Exception:
            logger.debug("scan set: token cache unavailable", exc_info=True)
        return sorted(out)

    def _eth_call(self, *, chain: str, address: str, signature: str, args) -> Optional[str]:
        from tools.defi.data_tool import CONTRACT_READ_GAS_CAP  # call-time
        if self._call_fn is not None:
            return self._call_fn(chain=chain, address=address, signature=signature, args=args)
        from eth_utils import keccak
        from core.wallet.onchain import _rpc, rpc_url_for_chain
        selector = "0x" + keccak(text=signature.replace(" ", "")).hex()[:8]
        return _rpc(rpc_url_for_chain(chain), "eth_call",
                    [{"to": address, "data": selector, "gas": hex(CONTRACT_READ_GAS_CAP)},
                     "latest"], 8.0)

    @staticmethod
    def _best_effort_decode(raw: str) -> Optional[str]:
        if not raw or raw == "0x":
            return None
        body = raw[2:]
        if len(body) == 64:
            try:
                return str(int(body, 16))
            except ValueError:
                return None
        return None
