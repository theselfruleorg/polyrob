"""Sign-In with Ethereum (SIWE) authentication - FREE alternative to Privy.

SIWE is the industry standard for wallet authentication:
- Used by ENS, OpenSea, and many others
- Completely free, no usage limits
- Works with any wallet
- Secure by design with nonce management
"""

import logging
from datetime import datetime, timedelta
from typing import Optional
import secrets
import hashlib
from eth_account.messages import encode_defunct
from web3 import Web3

logger = logging.getLogger(__name__)


class SIWEAuthenticator:
    """
    Free wallet authentication using SIWE (Sign-In with Ethereum).

    This replaces Privy with a completely free solution.
    """

    def __init__(self, db):
        """
        Initialize SIWE authenticator.

        Args:
            db: Database manager instance
        """
        self.db = db
        self.w3 = Web3()
        self.logger = logging.getLogger('auth.siwe')

    async def generate_nonce(self, wallet_address: str, chain_id: int = 1) -> str:
        """
        Generate authentication nonce for wallet.

        Nonces prevent replay attacks. The nonce is bound to the chain_id it
        was issued for, so a signed message declaring a different chain can
        never be validated against it (see _verify_nonce).

        Args:
            wallet_address: Ethereum wallet address
            chain_id: Blockchain ID this nonce is being issued for (default: 1 = Ethereum mainnet)

        Returns:
            Random nonce string
        """
        nonce = secrets.token_hex(32)

        # Store nonce with expiration (5 minutes)
        await self.db.execute("""
            INSERT OR REPLACE INTO auth_nonces (
                wallet_address, nonce, chain_id, expires_at
            ) VALUES (?, ?, ?, datetime('now', '+5 minutes'))
        """, (wallet_address.lower(), nonce, chain_id))

        self.logger.debug(f"Generated nonce for {wallet_address[:8]}... (chain {chain_id})")

        return nonce

    async def verify_signature(
        self,
        wallet_address: str,
        message: str,
        signature: str,
        nonce: Optional[str] = None
    ) -> bool:
        """
        Verify wallet signature.

        Args:
            wallet_address: Ethereum wallet address
            message: Signed message
            signature: Signature from wallet
            nonce: Optional nonce to verify (prevents replay attacks)

        Returns:
            True if signature is valid and nonce is correct
        """

        try:
            # 1. Verify signature cryptographically
            message_hash = encode_defunct(text=message)
            recovered_address = self.w3.eth.account.recover_message(
                message_hash,
                signature=signature
            )

            if recovered_address.lower() != wallet_address.lower():
                self.logger.warning(
                    f"Signature verification failed: expected {wallet_address[:8]}..., "
                    f"got {recovered_address[:8]}..."
                )
                return False

            # 2. Verify nonce if provided (prevents replay attacks), bound to the
            # chain_id declared in the submitted message (prevents chain-ID replay:
            # a nonce issued for one chain must not validate a message claiming another).
            if nonce:
                submitted_chain_id = self._extract_chain_id(message)
                valid_nonce = await self._verify_nonce(wallet_address, nonce, chain_id=submitted_chain_id)
                if not valid_nonce:
                    self.logger.warning(f"Invalid or expired nonce for {wallet_address[:8]}...")
                    return False

                # Consume nonce (one-time use)
                await self._consume_nonce(wallet_address, nonce)

            # 3. Check message freshness (5 minute window)
            if not self._check_message_freshness(message):
                self.logger.warning(f"Message expired for {wallet_address[:8]}...")
                return False

            self.logger.info(f"Successfully verified signature for {wallet_address[:8]}...")
            return True

        except Exception as e:
            self.logger.error(f"Signature verification error: {e}")
            return False

    async def create_siwe_message(
        self,
        wallet_address: str,
        domain: str,
        uri: str,
        chain_id: int = 1  # Ethereum mainnet by default
    ) -> dict:
        """
        Create SIWE-compliant authentication message.

        SIWE format: https://eips.ethereum.org/EIPS/eip-4361

        Args:
            wallet_address: User's wallet address
            domain: Your domain (e.g., "app.your-polyrob-host.example")
            uri: Your app URI
            chain_id: Blockchain ID (1=Ethereum, 137=Polygon, 8453=Base, 42161=Arbitrum)

        Returns:
            Dict with message and nonce
        """

        nonce = await self.generate_nonce(wallet_address, chain_id=chain_id)
        issued_at = datetime.utcnow().isoformat() + 'Z'
        expiration = (datetime.utcnow() + timedelta(minutes=5)).isoformat() + 'Z'

        # SIWE-compliant message format
        message = f"""{domain} wants you to sign in with your Ethereum account:
{wallet_address}

Sign in to POLYROB - AI Automation Platform

URI: {uri}
Version: 1
Chain ID: {chain_id}
Nonce: {nonce}
Issued At: {issued_at}
Expiration Time: {expiration}"""

        return {
            "message": message,
            "nonce": nonce,
            "issued_at": issued_at,
            "expiration": expiration
        }

    async def _verify_nonce(self, wallet_address: str, nonce: str, chain_id: Optional[int] = None) -> bool:
        """Verify nonce is valid, not expired, and — when the stored row is
        bound to a chain — was issued for that same chain.

        Fail CLOSED, keyed off the STORED row's chain_id (not the submitted
        chain_id): if the row has a concrete chain_id, the submitted message
        MUST declare the matching chain, INCLUDING the case where the
        submitted chain_id is None (the attacker simply omitted the
        `Chain ID:` line, or it failed to parse). Omitting the line is not a
        way to skip the check — it is trivially attacker-controlled request
        data. A legacy row with chain_id IS NULL (pre-migration) still skips
        the check, preserving the grace period; every row written by
        generate_nonce today always carries a concrete chain_id.
        """

        result = await self.db.fetch_one("""
            SELECT nonce, chain_id FROM auth_nonces
            WHERE wallet_address = ?
                AND nonce = ?
                AND expires_at > datetime('now')
                AND used = 0
        """, (wallet_address.lower(), nonce))

        if result is None:
            return False

        if result["chain_id"] is not None and result["chain_id"] != chain_id:
            self.logger.warning(
                f"Chain ID mismatch for {wallet_address[:8]}...: "
                f"issued for {result['chain_id']}, submitted {chain_id}"
            )
            return False

        return True

    @staticmethod
    def _extract_chain_id(message: str) -> Optional[int]:
        """Parse the `Chain ID:` line out of a SIWE message body."""
        for line in message.split('\n'):
            if line.startswith('Chain ID:'):
                try:
                    return int(line.split('Chain ID:')[1].strip())
                except ValueError:
                    return None
        return None

    async def _consume_nonce(self, wallet_address: str, nonce: str):
        """Mark nonce as used (one-time use)."""

        await self.db.execute("""
            UPDATE auth_nonces
            SET used = 1
            WHERE wallet_address = ? AND nonce = ?
        """, (wallet_address.lower(), nonce))

    def _check_message_freshness(self, message: str, max_age: int = 300) -> bool:
        """
        Check if message was created recently.

        Prevents replay attacks with old signatures.

        Args:
            message: The signed message
            max_age: Maximum age in seconds (default: 5 minutes)

        Returns:
            True if message is fresh
        """

        try:
            # Extract "Issued At" from SIWE message
            for line in message.split('\n'):
                if line.startswith('Issued At:'):
                    issued_at_str = line.split('Issued At:')[1].strip()
                    issued_at = datetime.fromisoformat(issued_at_str.replace('Z', ''))
                    age = (datetime.utcnow() - issued_at).total_seconds()
                    return age < max_age

            # Fallback: check for timestamp in message
            if 'timestamp:' in message.lower():
                timestamp_str = message.split('timestamp:')[-1].strip().split()[0]
                timestamp = int(timestamp_str)
                age = datetime.now().timestamp() - timestamp
                return age < max_age

        except Exception as e:
            self.logger.debug(f"Could not parse message timestamp: {e}")

        # If we can't verify freshness, reject (fail-safe)
        return False

    async def cleanup_expired_nonces(self):
        """Clean up expired nonces (run periodically)."""

        result = await self.db.execute("""
            DELETE FROM auth_nonces
            WHERE expires_at < datetime('now')
        """)

        if result.rowcount > 0:
            self.logger.info(f"Cleaned up {result.rowcount} expired nonces")
