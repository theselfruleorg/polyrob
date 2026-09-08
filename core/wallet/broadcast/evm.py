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

#: Hard ceiling on the total fee we will sign for, per transaction — the
#: FALLBACK for a chain whose row carries no value of its own. A fee above the
#: ceiling means a fee-market anomaly or a bad RPC, and signing through it could
#: burn the whole gas balance on one transaction. The live value is per chain
#: (`chains.ChainRow.max_fee_wei_per_tx`): one L2-sized number refused every
#: honest L1 transaction.
MAX_FEE_WEI_PER_TX = 2 * 10 ** 15          # 0.002 ETH
DEFAULT_GAS_LIMIT = 120_000
#: Ceiling on a SIZED gas limit (size_gas). A transaction whose simulation
#: already used more than this is refused outright: broadcasting it with less
#: gas would out-of-gas revert on-chain and burn the whole fee.
#:
#: Raised 500k -> 2M on 2026-08-25, after the first live aggregator entry was
#: refused here. It was sized for a direct Uniswap V3 ``exactInputSingle``
#: (130-190k). An aggregator route hops several pools through a Diamond proxy
#: and legitimately costs far more: the refused route measured **1,112,721 gas**
#: on Base. At 500k the multi-chain rail was reachable but not executable —
#: every quote clean, every swap refused — which is the same failure as a fee
#: ceiling that covers zero transactions.
#:
#: ⚠️ This is NOT the economic bound and must not be treated as one. What a
#: transaction may COST is ``ChainRow.max_fee_wei_per_tx``, re-checked against
#: the SIZED gas a few lines below in ``size_gas`` — so raising this number
#: widens what may EXECUTE, never what may be SPENT. On Base, 2M gas at the
#: measured 0.006 gwei is ~0.000012 ETH against a 0.002 ETH fee ceiling, so the
#: fee check still binds first by two orders of magnitude. This stays an anomaly
#: brake against a pathological simulation.
MAX_GAS_LIMIT = 2_000_000
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


def _rpc_call(chain: str, method: str, params: list, timeout: float = 8.0) -> Any:
    """Indirection point so tests can substitute the transport.

    ⚠️ *chain* is not decoration: this used to resolve the BASE endpoint
    unconditionally, so a rail for any other chain would read base's nonce and
    base's fee market and sign them for a different chain id.
    """
    return onchain._rpc(onchain.rpc_url_for_chain(chain), method, params, timeout)


def _hex_to_int(raw) -> Optional[int]:
    if raw is None or raw == "0x":
        return None
    try:
        return int(raw, 16)
    except (TypeError, ValueError):
        return None


class EvmRail:
    def __init__(self, chain: str, signer, *, gas_limit: int = DEFAULT_GAS_LIMIT):
        from core.wallet import chains
        row = chains.get(chain)
        if row is None:
            raise ValueError(f"unknown chain {chain!r} (known: {chains.names()})")
        if not row.money_enabled:
            # A readable chain is not a spendable one. Refusing at construction
            # keeps a rail that cannot build an honest transaction from ever
            # existing, rather than catching it later at broadcast.
            raise ValueError(
                f"chain {row.name!r} is read-only here — no value may move on "
                f"it. {row.purpose}")
        self.chain = row.name
        self.chain_id = row.chain_id            # config, NOT the RPC
        self._max_fee_wei = row.max_fee_wei_per_tx
        self._signer = signer
        self._gas_limit = gas_limit

    def _rpc(self, method: str, params: list, timeout: float = 8.0) -> Any:
        return _rpc_call(self.chain, method, params, timeout)

    # -- preflight --------------------------------------------------------
    def preflight(self):
        """(ok, reason). Verifies the node serves the chain we are pinned to."""
        try:
            served = _hex_to_int(self._rpc("eth_chainId", []))
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

    def build_call(self, *, to: str, data: str, value: int = 0) -> dict:
        """A transaction carrying caller-supplied calldata (023 T4: approve/swap).

        This builds ONLY — it grants no authority. Everything built here still
        goes through ``tx_guard.authorize``, which simulates the call and reads
        the REAL asset and allowance deltas; a hidden approve or an unexpected
        outflow is refused there regardless of what this calldata claims to do.
        """
        from core.wallet.tokens import normalize_address
        if not isinstance(data, str) or not data.startswith("0x"):
            raise BroadcastError("calldata must be a 0x-prefixed hex string")
        return self._finish_tx(to_addr=normalize_address(to), data=data,
                               value=int(value))

    def _finish_tx(self, *, to_addr: str, data: str, value: int) -> dict:
        nonce = _hex_to_int(self._rpc("eth_getTransactionCount",
                                      [self._signer.address, "pending"]))
        if nonce is None:
            raise BroadcastError("could not read nonce")
        tip = _hex_to_int(self._rpc("eth_maxPriorityFeePerGas", [])) or 10 ** 8
        block = self._rpc("eth_getBlockByNumber", ["latest", False]) or {}
        base_fee = _hex_to_int(block.get("baseFeePerGas")) or 0
        max_fee = base_fee * 2 + tip

        gas = self._gas_limit
        total_fee = max_fee * gas
        if total_fee > self._max_fee_wei:
            raise GasCeilingExceeded(
                f"estimated max fee {total_fee} wei exceeds the {self.chain} "
                f"ceiling {self._max_fee_wei} wei — refusing to sign")

        return {
            "to": to_addr, "value": value, "data": data, "nonce": nonce,
            "gas": gas, "maxFeePerGas": max_fee, "maxPriorityFeePerGas": tip,
            "chainId": self.chain_id, "type": 2,
        }

    def size_gas(self, tx: dict, sim_gas_used: Optional[int]) -> dict:
        """Return *tx* with its gas limit sized from the simulation's gasUsed.

        A fixed limit is wrong in both directions: too low out-of-gas-reverts
        on-chain and burns the fee (a Uniswap V3 ``exactInputSingle`` often
        needs 130-190k against the old fixed 120k), too high inflates the
        worst-case fee. ×1.5 margin, because broadcast state differs slightly
        from simulated state (cold vs bundle-warmed storage slots, extra tick
        crossings). No measurement → the built default stands. The fee ceiling
        is re-checked against the sized gas — it was only checked against the
        default at build time.
        """
        if not sim_gas_used or sim_gas_used <= 0:
            return tx
        if sim_gas_used > MAX_GAS_LIMIT:
            raise GasCeilingExceeded(
                f"simulation used {sim_gas_used} gas, above the {MAX_GAS_LIMIT} "
                f"gas-limit ceiling — broadcasting with less would out-of-gas "
                f"revert on-chain and burn the fee; refusing to sign")
        sized = min(max(sim_gas_used * 3 // 2, 21_000), MAX_GAS_LIMIT)
        max_fee = int(tx.get("maxFeePerGas", 0) or 0)
        total_fee = max_fee * sized
        if total_fee > self._max_fee_wei:
            raise GasCeilingExceeded(
                f"sized gas {sized} at maxFeePerGas {max_fee} implies a max fee "
                f"of {total_fee} wei, above the {self.chain} ceiling "
                f"{self._max_fee_wei} wei — refusing to sign")
        return {**tx, "gas": sized}

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
        tx_hash = self._rpc("eth_sendRawTransaction", ["0x" + raw.hex()])
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
                rec = self._rpc("eth_getTransactionReceipt", [tx_hash])
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
