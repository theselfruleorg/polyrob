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


class ReputationManager:
    """Manages reputation/feedback for the ERC-8004 Reputation Registry."""
    
    def __init__(self):
        """Initialize the reputation manager."""
        self.config = get_eip8004_config()
        self._feedback_cache: Dict[str, List[FeedbackEntry]] = {}
        
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
            Submission result with transaction info
        """
        # Verify authorization
        if not self.verify_feedback_auth(feedback_auth):
            raise ValueError("Invalid or expired feedback authorization")
        
        # Validate score
        if not 0 <= score <= 100:
            raise ValueError("Score must be 0-100")
        
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
        
        logger.info(f"Feedback submitted for agent {agent_id}: score={score}")
        
        # In production, this would:
        # 1. Upload feedback_file to IPFS
        # 2. Call ReputationRegistry.submitFeedback() on-chain
        
        return {
            "success": True,
            "fileHash": f"0x{file_hash}",
            "agentId": agent_id,
            "score": score,
            "message": "Feedback recorded (local mode - on-chain submission pending)"
        }
    
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

