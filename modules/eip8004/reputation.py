"""ERC-8004 Reputation Registry Manager.

Handles feedback authorization, submission, and querying for the Reputation Registry.
"""

import os
import logging
import hashlib
import json
import time
from typing import Optional, List, Dict, Any
from datetime import datetime

from .models import (
    FeedbackAuth,
    FeedbackEntry,
    FeedbackFile,
    ReputationSummary,
    ProofOfPayment,
)
from .registration import get_eip8004_config

logger = logging.getLogger(__name__)

# Try to import eth_account for EIP-712 signing (optional)
# _ENCODE_FULL_MESSAGE: the modern API takes full_message=<dict>; the legacy
# encode_structured_data takes the dict positionally. Track which so we encode
# the SAME way on sign and verify (a mismatch silently broke signing before).
try:
    from eth_account import Account
    from eth_account.messages import encode_typed_data  # Updated API
    ETH_ACCOUNT_AVAILABLE = True
    _ENCODE_FULL_MESSAGE = True
except ImportError:
    try:
        from eth_account import Account
        from eth_account.messages import encode_structured_data as encode_typed_data
        ETH_ACCOUNT_AVAILABLE = True
        _ENCODE_FULL_MESSAGE = False
    except ImportError:
        ETH_ACCOUNT_AVAILABLE = False
        _ENCODE_FULL_MESSAGE = False
        logger.warning("eth_account not available - EIP-712 signing disabled")


def _encode_eip712(typed_data: Dict[str, Any]):
    """Encode EIP-712 typed data via whichever eth_account API is available."""
    if _ENCODE_FULL_MESSAGE:
        return encode_typed_data(full_message=typed_data)
    return encode_typed_data(typed_data)


#: Terminal x402 statuses that count as "paid" for proof-of-payment
#: verification — mirrors the status set `settled_unnotified_invoices` /
#: `unified_ledger.py` already treat as settled (`completed` = tx-attested,
#: `settled_no_tx` = owner-attested with no on-chain transaction recorded).
_SETTLED_STATUSES = ("completed", "settled_no_tx")


class ReputationManager:
    """Manages reputation/feedback for the ERC-8004 Reputation Registry."""

    def __init__(self, *, db=None):
        """Initialize the reputation manager.

        ``db`` (Task 15, Phase 4): optional injected database handle, threaded
        through to `_verify_payment_proof`'s x402 lookup (test seam — mirrors
        `SettlementWatcher`'s `db=` constructor param). Production leaves this
        `None`, and the x402 lookup resolves the real database service from
        the container the same way `modules.x402.invoicing` already does.
        """
        self.config = get_eip8004_config()
        self._feedback_cache: Dict[str, List[FeedbackEntry]] = {}
        self._db = db
        # Task 15 follow-up (Finding 1, fix pass 2 — reproduced bypass): a
        # settled x402 tx hash may back AT MOST ONE feedback submission,
        # period. In-memory set on this instance, matching `_feedback_cache`'s
        # existing maturity level (a local simulation, not yet IPFS/on-chain);
        # production wires a single long-lived `ReputationManager`
        # (`api/eip8004_endpoints.py`'s module-level singleton) so this is a
        # real per-process guard, not a no-op.
        #
        # Keyed on ``txHash.lower()`` ALONE — NOT ``(agentId, txHash)``. Pass
        # 1 scoped the key per-agent on the theory that "the same tx could
        # legitimately reference different agents." That theory was wrong for
        # this deployment (one scalar `EIP8004_AGENT_ID`, one process-level
        # `ReputationManager` singleton — there is no legitimate multi-agent
        # case), and worse, `agent_id` is CALLER-CONTROLLED request-body
        # input, never validated against the configured/authorized agent. A
        # caller could resubmit the SAME real settled txHash + the SAME
        # signed `feedback_auth`, varying `agent_id` (42, 7, 999, ...) each
        # time to land in a different guard bucket — minting unlimited
        # `verified_purchase` feedback off ONE payment. `submit_feedback` now
        # ALSO binds `agent_id` to the authoritative agent identity (see
        # below), which independently closes the same hole, but the guard
        # key itself must not depend on caller-supplied data either way: one
        # settled tx backs at most one feedback, globally.
        self._consumed_payment_proofs: set = set()

    async def create_feedback_auth(
        self,
        client_address: str,
        task_id: Optional[str] = None,
        expires_in_seconds: int = 86400,
    ) -> FeedbackAuth:
        """Create a signed authorization for a client to submit feedback.
        
        This is called by the agent after accepting a task to authorize
        the client to give feedback.
        
        Args:
            client_address: Client's wallet address
            task_id: Optional A2A task ID for context
            expires_in_seconds: How long the authorization is valid
            
        Returns:
            Signed FeedbackAuth
        """
        if not self.config.agent_id:
            raise ValueError("Agent ID not configured (EIP8004_AGENT_ID)")
        
        agent_wallet = self.config.agent_wallet
        if not agent_wallet:
            raise ValueError("Agent wallet not configured (EIP8004_AGENT_WALLET)")
        
        # Generate nonce
        nonce = hashlib.sha256(
            f"{client_address}:{task_id or ''}:{time.time()}".encode()
        ).hexdigest()[:16]
        
        expires_at = int(time.time()) + expires_in_seconds

        typed_data = self._build_typed_data(
            self.config.agent_id, client_address, expires_at, nonce
        )

        # Sign with the agent's private key. Fail CLOSED — never emit a "0x"
        # placeholder that downstream verification might wave through.
        if not ETH_ACCOUNT_AVAILABLE:
            raise ValueError("EIP8004 signing unavailable: eth_account not installed")
        private_key = os.environ.get("EIP8004_AGENT_PRIVATE_KEY")
        if not private_key:
            raise ValueError(
                "EIP8004 signing unavailable: EIP8004_AGENT_PRIVATE_KEY not configured"
            )
        try:
            signable = _encode_eip712(typed_data)
            signed = Account.sign_message(signable, private_key)
            signature = signed.signature.hex()
            if not signature.startswith("0x"):
                signature = "0x" + signature
        except Exception as e:
            logger.error(f"Failed to sign feedback auth: {e}")
            raise ValueError("EIP8004 signing failed")

        return FeedbackAuth(
            agentId=self.config.agent_id,
            clientAddress=client_address,
            expiresAt=expires_at,
            nonce=nonce,
            signature=signature,
        )

    def _build_typed_data(
        self, agent_id: int, client_address: str, expires_at: int, nonce: str
    ) -> Dict[str, Any]:
        """EIP-712 typed data — shared by sign + verify so they can't drift."""
        return {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "FeedbackAuth": [
                    {"name": "agentId", "type": "uint256"},
                    {"name": "clientAddress", "type": "address"},
                    {"name": "expiresAt", "type": "uint256"},
                    {"name": "nonce", "type": "string"},
                ],
            },
            "primaryType": "FeedbackAuth",
            "domain": {
                "name": "EIP8004ReputationRegistry",
                "version": "1",
                "chainId": self.config.chain_id,
                "verifyingContract": self.config.reputation_registry_address
                or "0x0000000000000000000000000000000000000000",
            },
            "message": {
                "agentId": agent_id,
                "clientAddress": client_address,
                "expiresAt": expires_at,
                "nonce": nonce,
            },
        }

    def verify_feedback_auth(self, auth: FeedbackAuth) -> bool:
        """Verify a feedback authorization signature — fail CLOSED.

        Recovers the EIP-712 signer and requires it to equal the configured
        agent wallet. Returns False on expiry, missing/placeholder signature,
        missing eth_account, recovery failure, or signer mismatch.
        """
        # Check expiration
        if auth.expiresAt < int(time.time()):
            logger.warning("Feedback auth expired")
            return False

        if not auth.signature or auth.signature == "0x":
            logger.warning("No signature in feedback auth")
            return False

        if not ETH_ACCOUNT_AVAILABLE:
            logger.warning("eth_account unavailable - cannot verify feedback auth (fail closed)")
            return False

        expected = self.config.agent_wallet
        if not expected:
            logger.warning("EIP8004_AGENT_WALLET not configured - cannot verify (fail closed)")
            return False

        try:
            typed_data = self._build_typed_data(
                auth.agentId, auth.clientAddress, auth.expiresAt, auth.nonce
            )
            signable = _encode_eip712(typed_data)
            recovered = Account.recover_message(signable, signature=auth.signature)
        except Exception as e:
            logger.warning(f"Feedback auth signature recovery failed: {e}")
            return False

        if recovered.lower() != expected.lower():
            logger.warning("Feedback auth signer does not match agent wallet")
            return False
        return True

    async def _verify_payment_proof(self, proof: ProofOfPayment) -> bool:
        """Task 15 (Phase 4) anti-sybil check: a submitted ``ProofOfPayment``
        is only accepted as a verified-purchase signal when it references a
        REAL, SETTLED x402 payment request that actually paid THIS agent's
        treasury — never trust the proof's own field values at face value.
        ``ProofOfPayment`` doesn't carry the invoice's ``request_id``, only
        its ``txHash``, so the lookup is by transaction hash
        (:func:`modules.x402.invoicing.get_payment_request_by_tx_hash`,
        unambiguous — `transaction_hash` is uniquely indexed).

        Fail CLOSED: any lookup error, missing row, non-terminal status
        (pending/settling/expired), or a ``toAddress`` that doesn't match the
        settled invoice's own recipient returns False — the caller then
        refuses the feedback submission rather than silently accepting an
        unverifiable proof.

        HONEST GUARANTEE (Task 15 follow-up, Finding 3): a True return means
        "a real, settled x402 payment that reached this agent's treasury
        exists and this specific proof has not already backed a feedback
        submission" (the replay guard lives in :meth:`submit_feedback`, not
        here) — it does NOT mean "the author of this feedback IS the payer".
        x402 invoices do not record the payer's wallet address, so payer
        IDENTITY is never cryptographically bound to the feedback
        submitter: whoever learns a settled ``txHash`` first (e.g. it's
        visible on-chain, or the true payer shares it) can be the one to
        redeem it as ``verified_purchase`` feedback. Do not treat
        ``verified_purchase`` as sybil-proof author identity — see the
        ``EIP8004_PAYMENT_FEEDBACK`` row in ``docs/CONFIGURATION.md`` for
        the full caveat.
        """
        if not proof or not proof.txHash:
            return False
        try:
            from modules.x402 import invoicing
        except Exception:
            logger.warning(
                "eip8004: x402 invoicing module unavailable — cannot verify "
                "payment proof (fail closed)"
            )
            return False
        try:
            row = await invoicing.get_payment_request_by_tx_hash(proof.txHash, db=self._db)
        except Exception:
            logger.warning(
                "eip8004: payment-proof lookup failed for tx %s — treating as "
                "unverified (fail closed)", proof.txHash, exc_info=True,
            )
            return False
        if not row:
            logger.warning(
                "eip8004: payment proof references tx %s which matches NO x402 "
                "payment request — rejecting as unverified (anti-sybil)",
                proof.txHash,
            )
            return False
        if row.get("status") not in _SETTLED_STATUSES:
            logger.warning(
                "eip8004: payment proof references tx %s / request %s which is "
                "NOT settled (status=%s) — rejecting as unverified (anti-sybil)",
                proof.txHash, row.get("request_id"), row.get("status"),
            )
            return False
        # Task 15 follow-up (Finding 2): the invoice row genuinely settled,
        # but that alone doesn't prove the proof references a payment that
        # reached THIS agent's treasury — cross-check `proof.toAddress`
        # against the invoice's own `recipient` (case-insensitive; the
        # `recipient` column is stored lower-cased on insert, see
        # `modules/x402/invoicing.py::create_payment_request`, and on-chain
        # addresses have no case-sensitive checksum requirement at the
        # comparison level used here).
        #
        # Strict-reject an empty/missing `toAddress` rather than skip the
        # check: the ONE code path that builds a `ProofOfPayment` today,
        # `modules/eip8004/payment_proof.py::proof_from_settled_invoice`,
        # ALWAYS populates `toAddress` from the settled invoice's own
        # `recipient` (never empty once a tx hash is present) — so an empty
        # `toAddress` on a submitted proof can only mean it was fabricated
        # or tampered with by an externally-submitted proof, not a gap in
        # the legitimate offer path.
        invoice_recipient = str(row.get("recipient") or "").strip()
        proof_to_address = str(proof.toAddress or "").strip()
        if not proof_to_address or not invoice_recipient:
            logger.warning(
                "eip8004: payment proof for tx %s has no toAddress (or the "
                "settled invoice has no recorded recipient) — rejecting as "
                "unverified (anti-sybil, cannot confirm treasury match)",
                proof.txHash,
            )
            return False
        if proof_to_address.lower() != invoice_recipient.lower():
            logger.warning(
                "eip8004: payment proof for tx %s claims toAddress=%s but "
                "the settled invoice's recipient is %s — rejecting as "
                "unverified (anti-sybil, treasury mismatch)",
                proof.txHash, proof_to_address, invoice_recipient,
            )
            return False
        return True

    async def submit_feedback(
        self,
        agent_id: int,
        score: int,
        feedback_auth: FeedbackAuth,
        tag1: Optional[str] = None,
        tag2: Optional[str] = None,
        skill: Optional[str] = None,
        task_id: Optional[str] = None,
        proof_of_payment: Optional[ProofOfPayment] = None,
        comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Submit feedback for an agent.
        
        This can be called by clients after completing a task.
        
        Args:
            agent_id: Agent's on-chain ID
            score: Score 0-100
            feedback_auth: Agent's authorization
            tag1: Primary tag
            tag2: Secondary tag
            skill: A2A skill identifier
            task_id: A2A task ID
            proof_of_payment: x402 payment proof
            comment: Text comment

        Returns:
            Submission result with transaction info. When `proof_of_payment`
            was supplied and verified, the result carries `verified_purchase:
            True` (Task 15) — absent entirely when no proof was given, so the
            existing unverified path's response shape is unchanged.

            HONEST GUARANTEE (Task 15 follow-up, Finding 3):
            `verified_purchase: True` means a real, settled, non-replayed
            x402 payment that reached THIS agent's treasury is referenced by
            the proof. It does NOT mean this feedback's author IS the payer
            — x402 invoices do not record a payer wallet, so payer identity
            is never cryptographically bound to the feedback submitter. Do
            not treat `verified_purchase` as sybil-proof author identity;
            see the `EIP8004_PAYMENT_FEEDBACK` row in `docs/CONFIGURATION.md`.

        Raises:
            ValueError: invalid/expired authorization, an out-of-range score,
                (Task 15) a `proof_of_payment` that does NOT reference a
                real, settled x402 payment request that reached this agent's
                treasury (anti-sybil — a proof is verified, never trusted at
                face value), (Task 15 follow-up, Finding 1) a
                `proof_of_payment` whose `txHash` has ALREADY backed ANY
                prior feedback submission — one settled payment authorizes
                at most one verified-purchase feedback entry, globally
                (replay guard), OR (Task 15 fix pass 2 — reproduced bypass)
                a caller-supplied `agent_id` that does not match the
                configured `EIP8004_AGENT_ID` and/or the authorized
                `feedback_auth.agentId` (identity binding — `agent_id` is
                request-body input and must never be trusted as an
                arbitrary sybil-selectable label).
        """
        # Verify authorization
        if not self.verify_feedback_auth(feedback_auth):
            raise ValueError("Invalid or expired feedback authorization")

        # Task 15 fix pass 2 (reproduced bypass): bind the caller-supplied
        # `agent_id` to the authoritative agent identity. Pass 1 verified the
        # SIGNATURE on `feedback_auth` but never checked that `agent_id` (raw
        # request-body input) actually matches who that signature authorizes
        # — so a caller could submit real, verified feedback under ANY
        # `agent_id` it liked, misattributing it into an arbitrary agent's
        # `_feedback_cache` bucket (and, combined with the old per-agent
        # replay-guard key, replaying one settled tx across many `agent_id`
        # values). Two independent checks, either sufficient on its own:
        #   1. If this deployment has a configured agent
        #      (`EIP8004_AGENT_ID`), the submission must be FOR that agent.
        #   2. The submission must be FOR the agent the signed authorization
        #      actually names (`feedback_auth.agentId` is a required field —
        #      always present once `feedback_auth` parses).
        # There is no legitimate multi-agent case here: one scalar
        # `EIP8004_AGENT_ID` env var, one process-level `ReputationManager`
        # singleton per deployment.
        configured_agent_id = self.config.agent_id
        if configured_agent_id is not None and agent_id != configured_agent_id:
            raise ValueError(
                f"agent_id {agent_id} does not match the configured agent "
                f"identity {configured_agent_id} (EIP8004_AGENT_ID) — "
                "refusing to attribute feedback to an unauthorized agent"
            )
        if agent_id != feedback_auth.agentId:
            raise ValueError(
                f"agent_id {agent_id} does not match the authorized "
                f"agentId {feedback_auth.agentId} in the signed feedback "
                "authorization — refusing to attribute feedback to an "
                "unauthorized agent"
            )

        # Validate score
        if not 0 <= score <= 100:
            raise ValueError("Score must be 0-100")

        # Task 15 (Phase 4): a supplied payment proof must reference a REAL,
        # SETTLED x402 payment request before it's accepted as a
        # "verified-purchase" signal — otherwise this is a reputation-poisoning
        # sybil vector (fabricate a plausible-looking proof, get a boosted
        # score). Feedback with NO proof is unaffected (existing unverified path).
        verified_purchase: Optional[bool] = None
        proof_key: Optional[str] = None
        if proof_of_payment is not None:
            if not await self._verify_payment_proof(proof_of_payment):
                raise ValueError(
                    "payment proof does not reference a settled x402 payment "
                    "request — refusing to accept as verified-purchase feedback"
                )
            # Task 15 follow-up (Finding 1, fix pass 2): a verified proof is
            # real, but nothing above stops it being replayed to mint
            # UNLIMITED feedback entries off the SAME settled tx.
            # One-proof-one-feedback: key on ``txHash`` ALONE — NOT
            # ``(agent_id, txHash)``. Pass 1's per-agent key was the bypass:
            # `agent_id` is caller-controlled and the identity-binding check
            # above didn't exist yet, so varying `agent_id` across
            # resubmissions of the SAME tx landed in a different guard
            # bucket every time. There is no legitimate multi-agent case in
            # this deployment, so a global txHash-only key adds no lost
            # capability. Checked here, BEFORE `verified_purchase` is set;
            # only recorded as consumed once the feedback is actually
            # accepted below (no `await` occurs between this check and that
            # point, so two concurrent submissions racing the same tx can't
            # both pass).
            proof_key = proof_of_payment.txHash.strip().lower()
            if proof_key in self._consumed_payment_proofs:
                raise ValueError(
                    "payment proof already used for feedback — a settled "
                    "payment authorizes at most one verified-purchase "
                    "feedback submission (anti-sybil replay guard)"
                )
            verified_purchase = True

        # Build off-chain feedback file
        feedback_file = FeedbackFile(
            agentRegistry=f"eip155:{self.config.chain_id}:{self.config.identity_registry_address or '0x0'}",
            agentId=agent_id,
            clientAddress=feedback_auth.clientAddress,
            feedbackAuth=feedback_auth.signature,
            score=score,
            tag1=tag1,
            tag2=tag2,
            skill=skill,
            task=task_id,
            proof_of_payment=proof_of_payment,
            comment=comment,
        )
        
        # Store feedback (in-memory for now, would be IPFS + on-chain in production)
        file_content = feedback_file.model_dump_json()
        file_hash = hashlib.sha256(file_content.encode()).hexdigest()
        
        # Store in local cache
        if agent_id not in self._feedback_cache:
            self._feedback_cache[agent_id] = []
        
        entry = FeedbackEntry(
            agentId=agent_id,
            clientAddress=feedback_auth.clientAddress,
            score=score,
            tag1=tag1,
            tag2=tag2,
            fileUri=f"local://{file_hash}",  # Would be IPFS in production
            fileHash=f"0x{file_hash}",
            feedbackAuth=feedback_auth,
        )
        self._feedback_cache[agent_id].append(entry)

        # Task 15 follow-up (Finding 1): only now that the feedback has
        # actually been accepted and recorded do we mark the proof consumed
        # — a rejected/aborted submission never burns the payer's one shot.
        if proof_key is not None:
            self._consumed_payment_proofs.add(proof_key)

        logger.info(f"Feedback submitted for agent {agent_id}: score={score}")
        
        # In production, this would:
        # 1. Upload feedback_file to IPFS
        # 2. Call ReputationRegistry.submitFeedback() on-chain
        
        result = {
            "success": True,
            "fileHash": f"0x{file_hash}",
            "agentId": agent_id,
            "score": score,
            "message": "Feedback recorded (local mode - on-chain submission pending)"
        }
        if verified_purchase is not None:
            result["verified_purchase"] = verified_purchase
        return result
    
    async def get_reputation(
        self,
        agent_id: int,
        client_address: Optional[str] = None,
        tag1: Optional[str] = None,
        tag2: Optional[str] = None,
    ) -> ReputationSummary:
        """Get reputation summary for an agent.
        
        Args:
            agent_id: Agent's on-chain ID
            client_address: Filter by specific client
            tag1: Filter by tag1
            tag2: Filter by tag2
            
        Returns:
            ReputationSummary with scores and stats
        """
        # Get feedback entries (from cache, would be on-chain query in production)
        entries = self._feedback_cache.get(agent_id, [])
        
        # Apply filters
        if client_address:
            entries = [e for e in entries if e.clientAddress.lower() == client_address.lower()]
        if tag1:
            entries = [e for e in entries if e.tag1 == tag1]
        if tag2:
            entries = [e for e in entries if e.tag2 == tag2]
        
        # Calculate stats
        scores = [e.score for e in entries]
        avg_score = sum(scores) / len(scores) if scores else 0.0
        
        # Get top tags
        tag_counts: Dict[str, int] = {}
        for e in entries:
            if e.tag1:
                tag_counts[e.tag1] = tag_counts.get(e.tag1, 0) + 1
            if e.tag2:
                tag_counts[e.tag2] = tag_counts.get(e.tag2, 0) + 1
        
        top_tags = sorted(tag_counts.keys(), key=lambda t: tag_counts[t], reverse=True)[:5]
        
        return ReputationSummary(
            agentId=agent_id,
            totalFeedback=len(entries),
            averageScore=round(avg_score, 2),
            recentScores=scores[-10:] if scores else [],
            topTags=top_tags,
        )
    
    async def get_feedback_list(
        self,
        agent_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Get list of feedback entries for an agent.
        
        Args:
            agent_id: Agent's on-chain ID
            limit: Maximum entries to return
            offset: Offset for pagination
            
        Returns:
            List of feedback entry dicts
        """
        entries = self._feedback_cache.get(agent_id, [])
        paginated = entries[offset:offset + limit]
        
        return [
            {
                "score": e.score,
                "clientAddress": e.clientAddress,
                "tag1": e.tag1,
                "tag2": e.tag2,
                "fileHash": e.fileHash,
            }
            for e in paginated
        ]

