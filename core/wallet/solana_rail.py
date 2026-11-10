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
  ⚠️ That bound holds only for a RECENT-BLOCKHASH transaction. A durable-nonce
  transaction (first instruction System `AdvanceNonceAccount`) never expires,
  so it is refused here and in `solana_tx_inspect` (CR-M05).
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


class SolanaRejected(SolanaBroadcastError):
    """The node REFUSED the bytes and did not forward them: nothing was sent."""


#: JSON-RPC codes on ``sendTransaction`` that prove the node did NOT forward
#: the bytes: -32002 the preflight simulation failed (``BlockhashNotFound``
#: included), -32003 the signature verification failed. Every other code, and
#: every transport error, stays UNKNOWN. ⚠️ A preflight ``AlreadyProcessed``
#: means the bytes LANDED, so it is never a refusal.
_NOT_FORWARDED_CODES = frozenset({-32002, -32003})


def definitive_rejection(exc: BaseException) -> Optional[str]:
    """The node's refusal when *exc* proves ``sendTransaction`` forwarded
    nothing, else None (the CR-L01 mirror of ``broadcast.evm``)."""
    from core.wallet.solana_onchain import SolanaRpcError
    err = exc if isinstance(exc, SolanaRpcError) else exc.__cause__
    if not isinstance(err, SolanaRpcError) or err.code not in _NOT_FORWARDED_CODES:
        return None
    text = f"{err.message} {err.data}".lower()
    if "alreadyprocessed" in text or "already been processed" in text:
        return None
    return f"{err.code}: {err.message}"


#: The prefix ``confirm()`` stamps on a landed-but-failed status. A caller has
#: to tell a REVERT (it happened, it failed, the fee was paid) apart from an
#: UNKNOWN (the cluster has not seen it — usually an expired blockhash): the
#: two demand different next moves, and neither may read as a success. Kept
#: here, next to the producer, so no caller has to sniff for its own string.
LANDED_FAILED_PREFIX = "landed but FAILED on-chain"


#: The prefix ``confirm()`` stamps when the cluster never saw the signature AND
#: its blockhash has expired: the transaction can no longer land. Only then.
EXPIRED_PREFIX = "EXPIRED — never landed"

#: How long ``confirm()`` waits, after its polls, for an unseen signature's
#: blockhash to expire (~150 slots is about a minute; this is the bound).
EXPIRY_WAIT_SEC = 120.0


def confirmation_outcome(ok: bool, detail: str) -> str:
    """``"confirmed"`` | ``"reverted"`` | ``"expired"`` | ``"unknown"`` for a
    ``confirm()`` pair. ``expired`` = proven never landed; ``unknown`` = not
    known yet (it may still land)."""
    if ok:
        return "confirmed"
    text = str(detail or "")
    if text.startswith(LANDED_FAILED_PREFIX):
        return "reverted"
    return "expired" if text.startswith(EXPIRED_PREFIX) else "unknown"


def _is_durable_nonce(message) -> bool:
    """True when the first instruction is System ``AdvanceNonceAccount`` —
    the runtime's own test for a nonce transaction."""
    instructions = list(message.instructions)
    if not instructions:
        return False
    first = instructions[0]
    keys = list(message.account_keys)
    index = int(first.program_id_index)
    if index >= len(keys) or str(keys[index]) != "11111111111111111111111111111111":
        return False
    data = bytes(first.data)
    return len(data) >= 4 and int.from_bytes(data[:4], "little") == 4


def _journal_blockhash(signature: str) -> Optional[str]:
    """The blockhash ``send_raw`` journaled with *signature*, or None."""
    try:
        from core.wallet.submission_journal import journal_path
        from core.wallet.submission_store import connection
        with connection(journal_path()) as db:
            if db is None:
                return None
            row = db.execute("SELECT nonce FROM submissions WHERE tx_hash=? AND chain='solana'",
                             (str(signature),)).fetchone()
        return str(row[0]) if row and row[0] else None
    except Exception:
        logger.warning("solana: could not read the journaled blockhash of %s",
                       signature, exc_info=True)
        return None


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
            durable = _is_durable_nonce(transaction.message)
        except Exception as exc:
            raise SolanaBroadcastError("refusing malformed or unsigned transaction") from exc
        if durable:
            raise SolanaBroadcastError(
                "refusing a durable-nonce transaction: it never expires, so a "
                "signed copy stays broadcastable indefinitely and 'not "
                "confirmed' could no longer mean 'expired'")
        from core.wallet.submission_journal import mark_booked as prepared_release
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
            rejected = definitive_rejection(exc)
            if rejected is None:
                unknown = SolanaBroadcastError(
                    f"submission outcome unknown for {signature}; reconcile before retrying")
                unknown.signature = signature   # a caller stamps it on its record
                raise unknown from exc
            # The node refused and forwarded nothing: release the interlock this
            # attempt took (the EVM rail's CR-L01). A failed release leaves the
            # interlock standing — conservative, never a free pass.
            try:
                prepared_release(signature)
            except Exception:
                logger.warning("could not release the interlock for rejected %s",
                               signature, exc_info=True)
            raise SolanaRejected(
                f"the node rejected the transaction ({rejected}) — not sent") from exc
        if str(sig) != signature:
            unknown = SolanaBroadcastError(
                f"RPC did not confirm the expected signature {signature}; reconcile before retrying")
            unknown.signature = signature
            raise unknown
        return signature

    def confirm(self, signature: str, *, attempts: int = 30,
                delay: float = 2.0, blockhash: Optional[str] = None,
                expiry_wait: float = EXPIRY_WAIT_SEC) -> Tuple[bool, str]:
        """``(ok, detail)``.

        Four outcomes, kept distinct because they demand different next moves:
        confirmed-and-clean, confirmed-but-REVERTED (a landed failure, not a
        success), EXPIRED (never landed, and its blockhash is past, so it never
        can), and UNKNOWN — not seen yet, it may still land. Never retry an
        unknown blindly.

        ⚠️ "Not seen" after the polls is NOT final while the blockhash is still
        valid: the verbs have already booked the send and released the
        interlock, so a re-run in that window could pay twice. An unseen
        signature waits (bounded by *expiry_wait*) until its blockhash expired,
        then ONE final ``getSignatureStatuses(searchTransactionHistory)``
        decides: still None = EXPIRED. The blockhash comes from *blockhash*, or
        from the submission journal row ``send_raw`` wrote.
        """
        last, unseen = "no status", False
        for _ in range(max(1, attempts)):
            found, last, unseen = self._status(signature)
            if found is not None:
                return found
            if delay:
                time.sleep(delay)
        if not unseen:
            return False, last
        blockhash = blockhash or _journal_blockhash(signature)
        if not blockhash:
            return False, last
        deadline = time.monotonic() + max(0.0, float(expiry_wait))
        expired = False
        while time.monotonic() < deadline:
            try:
                res = self._rpc("isBlockhashValid",
                                [blockhash, {"commitment": self.COMMITMENT}])
                valid = (res or {}).get("value") if isinstance(res, dict) else None
            except Exception:
                valid = None                  # unreadable is never "expired"
            if valid is False:
                expired = True
                break
            found, last, unseen = self._status(signature)
            if found is not None:
                return found
            if delay:
                time.sleep(delay)
        if not expired:
            return False, last
        found, last, unseen = self._status(signature)
        if found is not None:
            return found
        if unseen:
            return False, (f"{EXPIRED_PREFIX}: the cluster never saw this signature and "
                           f"its blockhash has expired, so it can no longer land. "
                           f"Nothing moved and no fee was paid.")
        return False, last

    def _status(self, signature: str):
        """One status read: ``(final (ok, detail) | None, detail, unseen)``."""
        try:
            res = self._rpc("getSignatureStatuses", [[signature],
                                                     {"searchTransactionHistory": True}])
        except Exception as exc:
            return None, f"status read failed: {exc}", False
        entry = ((res or {}).get("value") or [None])[0]
        if entry is None:
            return None, ("UNKNOWN — the cluster has not seen this signature. "
                          "Most often the blockhash expired and it never "
                          "landed; do NOT resend without checking."), True
        if entry.get("err") is not None:
            return (False, (f"{LANDED_FAILED_PREFIX}: err="
                            f"{entry['err']}. The fee was still paid.")), "", False
        if entry.get("confirmationStatus") in ("confirmed", "finalized"):
            return (True, str(entry.get("confirmationStatus"))), "", False
        return None, f"pending ({entry.get('confirmationStatus')})", False

    def __repr__(self) -> str:
        return f"<SolanaRail signer={getattr(self._signer, 'address', '?')}>"
