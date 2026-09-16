"""Solana broadcast rail. Phase 3 — the counterpart to `broadcast/evm.py`.

Same job (build, sign, send, confirm), different model, and two differences are
load-bearing rather than cosmetic:

* **A recent blockhash replaces `chainId` as the replay bound.** An EVM
  transaction with no `chainId` is replayable on every chain the address exists
  on, which is why `LocalEoaSigner` refuses to sign one. A Solana transaction
  instead commits to a blockhash and is valid for only ~150 slots (about a
  minute). That bounds replay for free — but it also means a transaction can
  silently EXPIRE, so "not confirmed" here is far more often "expired" than
  "pending", and must never be retried blind.
* **`null` status is UNKNOWN, not failure.** `getSignatureStatuses` returns
  `null` for a signature the cluster has not seen. Reporting that as a failure
  is how a double-send happens.

Fee sizing is deliberately NOT modelled the way EVM gas is: the base fee is a
flat 5000 lamports per signature and the variable part is a priority fee the
caller chooses. There is no `gasUsed` to size against, so there is no analogue
of `size_gas` — the compute-unit LIMIT comes from the simulation instead.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)


class SolanaBroadcastError(RuntimeError):
    pass


#: The prefix ``confirm()`` stamps on a landed-but-failed status. A caller has
#: to tell a REVERT (it happened, it failed, the fee was paid) apart from an
#: UNKNOWN (the cluster has not seen it — usually an expired blockhash): the
#: two demand different next moves, and neither may read as a success. Kept
#: here, next to the producer, so no caller has to sniff for its own string.
LANDED_FAILED_PREFIX = "landed but FAILED on-chain"


def confirmation_outcome(ok: bool, detail: str) -> str:
    """``"confirmed"`` | ``"reverted"`` | ``"unknown"`` for a ``confirm()`` pair."""
    if ok:
        return "confirmed"
    return ("reverted" if str(detail or "").startswith(LANDED_FAILED_PREFIX)
            else "unknown")


class SolanaRail:
    def __init__(self, *, signer, rpc: Optional[Callable] = None,
                 enforce_registry: bool = False):
        """*enforce_registry* mirrors ``EvmRail``: refuse to exist for a chain
        the registry has not armed. Defaults False so Phase 3 can be developed
        and tested before the chain is armed; the tool-level door still asks
        ``chains.money_capable`` either way."""
        if enforce_registry:
            from core.wallet import chains
            ok, why = chains.money_capable("solana")
            if not ok:
                raise ValueError(why)
        self._signer = signer
        self._rpc_fn = rpc

    # -- rpc ---------------------------------------------------------------
    def _rpc(self, method: str, params: list, timeout: float = 10.0):
        if self._rpc_fn is not None:
            return self._rpc_fn(method, params)
        from core.wallet.solana_onchain import _rpc
        try:
            return _rpc(method, params, timeout)
        except Exception as exc:
            raise SolanaBroadcastError(f'{method}: RPC failed ({type(exc).__name__})') from exc

    #: The commitment the blockhash is read at, AND the one preflight must
    #: simulate against. ⚠️ ONE constant on purpose: these are not two settings,
    #: they are the same setting read twice, and letting them drift is the bug.
    #:
    #: Live 2026-09-13: the blockhash was fetched at `confirmed` while
    #: `sendTransaction` omitted `preflightCommitment`, whose RPC default is
    #: `finalized`. Preflight therefore simulated against a bank ~31 slots
    #: (~12s) older than the blockhash it was handed, so the node answered
    #: `BlockhashNotFound` about a blockhash it had issued seconds earlier.
    #: Measured on the pinned RPC that day: isBlockhashValid(@confirmed)=True,
    #: isBlockhashValid(@finalized)=False for the same hash.
    #:
    #: Two SOL->Base bridge broadcasts died on this, one second after the fetch
    #: -- far too fast for the expiry the error text implies, which is why it
    #: read as a flaky network and got retried instead of fixed.
    COMMITMENT = "confirmed"

    # -- build -------------------------------------------------------------
    def recent_blockhash(self) -> str:
        """The blockhash a transaction must commit to, or raise.

        Never invented on failure: a made-up blockhash produces a transaction
        that can never land, and it hides the RPC outage that caused it.
        """
        try:
            res = self._rpc("getLatestBlockhash", [{"commitment": self.COMMITMENT}])
        except SolanaBroadcastError:
            raise
        except Exception as exc:
            raise SolanaBroadcastError(f"could not read a recent blockhash: {exc}") from exc
        value = (res or {}).get("value") or {}
        blockhash = value.get("blockhash")
        if not blockhash:
            raise SolanaBroadcastError(
                "the RPC returned no recent blockhash — refusing to build a "
                "transaction without one (an invented blockhash can never land)")
        return str(blockhash)

    # -- send / confirm ----------------------------------------------------
    def send_raw(self, raw: bytes) -> str:
        """Broadcast pre-signed bytes and return the signature."""
        if not raw:
            raise SolanaBroadcastError("refusing to send an empty/unsigned transaction")
        import base64
        # Derive the public recovery identifier BEFORE contacting an RPC. A lost
        # response must never erase evidence of a possibly accepted submission.
        try:
            from solders.transaction import VersionedTransaction
            transaction = VersionedTransaction.from_bytes(bytes(raw))
            transaction.verify_and_hash_message()
            signature = str(transaction.signatures[0])
            blockhash = str(transaction.message.recent_blockhash)
        except Exception as exc:
            raise SolanaBroadcastError("refusing malformed or unsigned transaction") from exc
        from core.wallet.submission_journal import prepare
        prepare(signature, "solana", self._signer.address, blockhash)
        encoded = base64.b64encode(bytes(raw)).decode()
        try:
            sig = self._rpc("sendTransaction",
                            [encoded, {"encoding": "base64",
                                       "skipPreflight": False,
                                       # Preflight stays ON -- it is the last
                                       # check before a live send. It simply has
                                       # to simulate against the SAME bank the
                                       # blockhash came from; see COMMITMENT.
                                       "preflightCommitment": self.COMMITMENT,
                                       "maxRetries": 0}])
        except Exception as exc:
            raise SolanaBroadcastError(
                f"submission outcome unknown for {signature}; reconcile before retrying"
            ) from exc
        if str(sig) != signature:
            raise SolanaBroadcastError(
                f"RPC did not confirm the expected signature {signature}; reconcile before retrying")
        return signature

    def confirm(self, signature: str, *, attempts: int = 30,
                delay: float = 2.0) -> Tuple[bool, str]:
        """``(ok, detail)``.

        Three outcomes, kept distinct because they demand different next moves:
        confirmed-and-clean, confirmed-but-REVERTED (a landed failure, not a
        success), and UNKNOWN — the cluster has not seen it, which usually means
        the blockhash expired. Never retry an unknown blindly.
        """
        last = "no status"
        for _ in range(max(1, attempts)):
            try:
                res = self._rpc("getSignatureStatuses", [[signature],
                                                         {"searchTransactionHistory": True}])
            except Exception as exc:
                last = f"status read failed: {exc}"
            else:
                entry = ((res or {}).get("value") or [None])[0]
                if entry is None:
                    last = ("UNKNOWN — the cluster has not seen this signature. "
                            "Most often the blockhash expired and it never "
                            "landed; do NOT resend without checking.")
                else:
                    if entry.get("err") is not None:
                        return False, (f"{LANDED_FAILED_PREFIX}: err="
                                       f"{entry['err']}. The fee was still paid.")
                    if entry.get("confirmationStatus") in ("confirmed", "finalized"):
                        return True, str(entry.get("confirmationStatus"))
                    last = f"pending ({entry.get('confirmationStatus')})"
            if delay:
                time.sleep(delay)
        return False, last

    def __repr__(self) -> str:
        return f"<SolanaRail signer={getattr(self._signer, 'address', '?')}>"
