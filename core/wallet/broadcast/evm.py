"""EVM broadcast rail — build, sign, send, confirm.

This is the only code in the agent wallet that can move value on-chain. It is
deliberately dumb about policy: it does not decide whether a transaction is
allowed. `core.wallet.tx_guard.authorize()` does that, and the tool layer may
not call this module without a `Decision(allowed=True)`.

Two honesty rules the rest of the system depends on:

* **The chain is pinned from config, never read from the RPC.** A repointed or
  hostile endpoint must not be able to decide which chain we sign for — the same
  EOA exists on every EVM chain, so a tx signed for the wrong one can be
  replayed or land somewhere unintended. `preflight()` additionally verifies the
  node actually serves the expected chain and refuses if not.
* **A transaction is not "sent, assume fine".** A receipt with `status == 0` is
  FAILED, and a receipt that never arrives is PENDING — an open question the
  caller must surface, never a success.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from core.wallet import onchain

logger = logging.getLogger(__name__)

#: chain -> chain id. Pinned; the RPC is never the authority for this.
CHAIN_IDS = {"base": 8453, "arbitrum": 42161, "polygon": 137}

#: Hard ceiling on the total fee we will sign for, per transaction. Base is a
#: cheap L2 — a fee above this means a fee-market anomaly or a bad RPC, and
#: signing through it could burn the whole gas balance on one transaction.
MAX_FEE_WEI_PER_TX = 2 * 10 ** 15          # 0.002 ETH
DEFAULT_GAS_LIMIT = 120_000
_ERC20_TRANSFER_SELECTOR = "0xa9059cbb"


class GasCeilingExceeded(RuntimeError):
    """The estimated fee exceeds MAX_FEE_WEI_PER_TX; refusing to sign."""


class BroadcastError(RuntimeError):
    """The transaction could not be submitted."""


@dataclass(frozen=True)
class Receipt:
    tx_hash: str
    status: str                       # "success" | "failed" | "pending"
    block_number: Optional[int] = None
    gas_used: Optional[int] = None

    @property
    def succeeded(self) -> bool:
        return self.status == "success"


def _rpc_call(method: str, params: list, timeout: float = 8.0) -> Any:
    """Indirection point so tests can substitute the transport."""
    return onchain._rpc(onchain.rpc_url_for_chain("base"), method, params, timeout)


def _hex_to_int(raw) -> Optional[int]:
    if raw is None or raw == "0x":
        return None
    try:
        return int(raw, 16)
    except (TypeError, ValueError):
        return None


class EvmRail:
    def __init__(self, chain: str, signer, *, gas_limit: int = DEFAULT_GAS_LIMIT):
        if chain not in CHAIN_IDS:
            raise ValueError(f"unknown chain {chain!r} (known: {sorted(CHAIN_IDS)})")
        self.chain = chain
        self.chain_id = CHAIN_IDS[chain]        # config, NOT the RPC
        self._signer = signer
        self._gas_limit = gas_limit

    # -- preflight --------------------------------------------------------
    def preflight(self):
        """(ok, reason). Verifies the node serves the chain we are pinned to."""
        try:
            served = _hex_to_int(_rpc_call("eth_chainId", []))
        except Exception as exc:
            return False, f"rpc unreachable: {exc}"
        if served is None:
            return False, "rpc returned no chain id"
        if served != self.chain_id:
            return False, (f"rpc serves chain {served}, expected {self.chain_id} "
                           f"({self.chain}) — refusing to broadcast")
        return True, ""

    # -- build ------------------------------------------------------------
    def build_erc20_transfer(self, *, token: str, to: str, amount_raw: int) -> dict:
        """An ERC-20 transfer is a call TO the token contract, not to the payee."""
        from core.wallet.tokens import normalize_address
        token = normalize_address(token)
        to = normalize_address(to)
        data = (_ERC20_TRANSFER_SELECTOR
                + to[2:].lower().rjust(64, "0")
                + f"{int(amount_raw):064x}")
        return self._finish_tx(to_addr=token, data=data, value=0)

    def build_native_transfer(self, *, to: str, amount_wei: int) -> dict:
        from core.wallet.tokens import normalize_address
        return self._finish_tx(to_addr=normalize_address(to), data="0x",
                               value=int(amount_wei))

    def _finish_tx(self, *, to_addr: str, data: str, value: int) -> dict:
        nonce = _hex_to_int(_rpc_call("eth_getTransactionCount",
                                      [self._signer.address, "pending"]))
        if nonce is None:
            raise BroadcastError("could not read nonce")
        tip = _hex_to_int(_rpc_call("eth_maxPriorityFeePerGas", [])) or 10 ** 8
        block = _rpc_call("eth_getBlockByNumber", ["latest", False]) or {}
        base_fee = _hex_to_int(block.get("baseFeePerGas")) or 0
        max_fee = base_fee * 2 + tip

        gas = self._gas_limit
        total_fee = max_fee * gas
        if total_fee > MAX_FEE_WEI_PER_TX:
            raise GasCeilingExceeded(
                f"estimated max fee {total_fee} wei exceeds the ceiling "
                f"{MAX_FEE_WEI_PER_TX} wei — refusing to sign")

        return {
            "to": to_addr, "value": value, "data": data, "nonce": nonce,
            "gas": gas, "maxFeePerGas": max_fee, "maxPriorityFeePerGas": tip,
            "chainId": self.chain_id, "type": 2,
        }

    # -- send / confirm ---------------------------------------------------
    def sign_and_send(self, tx: dict) -> str:
        ok, why = self.preflight()
        if not ok:
            raise BroadcastError(why)
        if tx.get("chainId") != self.chain_id:
            raise BroadcastError(
                f"transaction chainId {tx.get('chainId')} != rail chain "
                f"{self.chain_id} — refusing")
        raw = self._signer.sign_transaction(tx)
        tx_hash = _rpc_call("eth_sendRawTransaction", ["0x" + raw.hex()])
        if not tx_hash:
            raise BroadcastError("node accepted no transaction hash")
        return tx_hash

    def await_receipt(self, tx_hash: str, *, timeout: float = 120.0,
                      poll_interval: float = 2.0) -> Receipt:
        """Poll for a receipt.

        Never optimistic: no receipt within the budget is PENDING (an open
        question carrying the hash), and status 0 is FAILED.
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                rec = _rpc_call("eth_getTransactionReceipt", [tx_hash])
            except Exception:
                rec = None
            if rec:
                status = _hex_to_int(rec.get("status"))
                return Receipt(
                    tx_hash=tx_hash,
                    status="success" if status == 1 else "failed",
                    block_number=_hex_to_int(rec.get("blockNumber")),
                    gas_used=_hex_to_int(rec.get("gasUsed")))
            if time.monotonic() >= deadline:
                logger.warning("broadcast: no receipt for %s within %ss — PENDING",
                               tx_hash, timeout)
                return Receipt(tx_hash=tx_hash, status="pending")
            if poll_interval:
                time.sleep(poll_interval)
