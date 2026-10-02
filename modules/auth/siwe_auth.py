"""Sign-In with Ethereum (SIWE) authentication - FREE alternative to Privy.

SIWE is the industry standard for wallet authentication:
- Used by ENS, OpenSea, and many others
- Completely free, no usage limits
- Works with any wallet
- Secure by design with nonce management
"""

import hmac
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse
import secrets
import hashlib
from eth_account.messages import encode_defunct
from web3 import Web3

logger = logging.getLogger(__name__)

#: The SIWE ``domain`` default — the same value ``/api/auth/nonce`` has always
#: stamped when ``WEBVIEW_DOMAIN`` is unset.
DEFAULT_SIWE_DOMAIN = "localhost:3000"

#: How far in the FUTURE an ``Issued At`` may sit (client clock skew). Beyond
#: it the message is refused: a future timestamp used to stay "fresh" forever.
ISSUED_AT_MAX_SKEW_S = 60

_SIWE_HEADER_RE = re.compile(
    r"^(?P<domain>\S+) wants you to sign in with your Ethereum account:$")
_SIWE_FIELDS = ("URI", "Version", "Chain ID", "Nonce", "Issued At",
                "Expiration Time", "Not Before", "Request ID")


def configured_siwe_domain() -> str:
    """The host this instance signs users in for (``WEBVIEW_DOMAIN``).

    ONE reader: ``/api/auth/nonce`` stamps it into the message and
    :meth:`SIWEAuthenticator.verify_signature` requires it back.
    """
    return (os.environ.get("WEBVIEW_DOMAIN") or DEFAULT_SIWE_DOMAIN).strip()


def parse_siwe_message(message: str) -> Optional[dict]:
    """Parse an EIP-4361 message into ``{domain, address, <Field>: value}``.

    Returns None for anything that is not the EIP-4361 shape: no header, no
    address line, a field block that does not start with ``URI:``, an
    unknown or DUPLICATED field. The field block is the text after the last
    blank line, so a ``Nonce:`` inside the statement can never be read as the
    nonce.
    """
    if not isinstance(message, str) or "\r" in message:
        return None
    lines = message.split("\n")
    if len(lines) < 4:
        return None
    m = _SIWE_HEADER_RE.match(lines[0])
    if not m:
        return None
    address = lines[1].strip()
    if not address or lines[2] != "":
        return None
    try:
        last_blank = max(i for i, ln in enumerate(lines) if ln == "")
    except ValueError:
        return None
    block = lines[last_blank + 1:]
    if not block or not block[0].startswith("URI: "):
        return None
    out: dict = {"domain": m.group("domain"), "address": address}
    for ln in block:
        if ln == "Resources:" or ln.startswith("- "):
            continue  # the optional resource list carries no binding field
        key, sep, value = ln.partition(": ")
        if not sep or key not in _SIWE_FIELDS or key in out:
            return None
        out[key] = value.strip()
    for required in ("URI", "Version", "Chain ID", "Nonce", "Issued At"):
        if not out.get(required):
            return None
    return out


def _parse_ts(value: str) -> datetime:
    """An RFC 3339 timestamp as an AWARE UTC datetime (naive reads as UTC)."""
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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
        nonce: Optional[str] = None,
        *,
        expected_domain: Optional[str] = None,
    ) -> bool:
        """
        Verify a signed EIP-4361 message.

        Security review 2026-09-23 (M11). The message is PARSED, and every
        binding field must hold:

        * ``domain`` == the configured host (``expected_domain``, else
          :func:`configured_siwe_domain`), and the ``URI`` authority is the
          same host;
        * the address line is ``wallet_address`` and the signer recovers to it;
        * ``Nonce:`` == the submitted ``nonce`` (required), which is consumed
          ATOMICALLY with the ``Chain ID`` check against the stored row;
        * ``Issued At`` is at most :data:`ISSUED_AT_MAX_SKEW_S` in the future
          and younger than 5 minutes; an ``Expiration Time`` is in the future.

        Returns True only when all hold. Never raises.
        """

        try:
            if not nonce:
                self.logger.warning("SIWE verify refused: no nonce submitted")
                return False
            fields = parse_siwe_message(message)
            if fields is None:
                self.logger.warning("SIWE verify refused: not an EIP-4361 message")
                return False

            # 1. Domain + URI bind the signature to THIS host.
            domain = (expected_domain or configured_siwe_domain()).strip().lower()
            if fields["domain"].lower() != domain:
                self.logger.warning(f"SIWE domain mismatch: {fields['domain']!r}")
                return False
            if (urlparse(fields["URI"]).netloc or "").lower() != domain:
                self.logger.warning(f"SIWE URI host mismatch: {fields['URI']!r}")
                return False
            if fields["address"].lower() != wallet_address.lower():
                self.logger.warning("SIWE address line does not match the wallet")
                return False
            if fields["Version"] != "1":
                return False

            # 2. The in-message nonce IS the submitted nonce.
            if not hmac.compare_digest(fields["Nonce"].encode(), str(nonce).encode()):
                self.logger.warning("SIWE nonce in message does not match the submitted nonce")
                return False
            try:
                chain_id = int(fields["Chain ID"])
            except ValueError:
                return False

            # 3. Freshness: no future Issued At beyond the skew, no stale message,
            # no passed Expiration Time.
            if not self._check_message_freshness(message):
                self.logger.warning(f"Message not fresh for {wallet_address[:8]}...")
                return False

            # 4. Verify signature cryptographically.
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

            # 5. Consume the nonce ATOMICALLY, bound to the chain it was issued
            # for: one UPDATE both checks and spends it, so two concurrent
            # verifies of one nonce cannot both succeed.
            if not await self._consume_nonce_atomic(wallet_address, nonce, chain_id):
                self.logger.warning(f"Invalid, used or expired nonce for {wallet_address[:8]}...")
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

    async def _consume_nonce_atomic(self, wallet_address: str, nonce: str,
                                    chain_id: Optional[int]) -> bool:
        """Check AND spend a nonce in one statement. True when it was live.

        Live = not used, not expired, and issued for ``chain_id`` (a legacy row
        with ``chain_id IS NULL`` keeps its grace period, as in
        :meth:`_verify_nonce`).
        """
        result = await self.db.execute("""
            UPDATE auth_nonces
            SET used = 1
            WHERE wallet_address = ?
                AND nonce = ?
                AND used = 0
                AND expires_at > datetime('now')
                AND (chain_id IS NULL OR chain_id = ?)
        """, (wallet_address.lower(), nonce, chain_id))
        return getattr(result, "rowcount", 0) == 1

    async def _consume_nonce(self, wallet_address: str, nonce: str):
        """Mark nonce as used (one-time use)."""

        await self.db.execute("""
            UPDATE auth_nonces
            SET used = 1
            WHERE wallet_address = ? AND nonce = ?
        """, (wallet_address.lower(), nonce))

    def _check_message_freshness(self, message: str, max_age: int = 300) -> bool:
        """
        Is the EIP-4361 message fresh?

        ``Issued At`` must be no more than :data:`ISSUED_AT_MAX_SKEW_S` in the
        future and younger than ``max_age`` seconds; an ``Expiration Time``,
        when present, must be in the future and a ``Not Before`` in the past.
        There is no other timestamp source: the old free-text ``timestamp:``
        fallback is gone (M11). Anything unparseable is NOT fresh.
        """

        try:
            fields = parse_siwe_message(message)
            if fields is None:
                return False
            now = datetime.now(timezone.utc)
            issued_at = _parse_ts(fields["Issued At"])
            age = (now - issued_at).total_seconds()
            if age < -ISSUED_AT_MAX_SKEW_S or age >= max_age:
                return False
            if fields.get("Expiration Time"):
                if _parse_ts(fields["Expiration Time"]) <= now:
                    return False
            if fields.get("Not Before"):
                if _parse_ts(fields["Not Before"]) > now + timedelta(seconds=ISSUED_AT_MAX_SKEW_S):
                    return False
            return True
        except Exception as e:
            self.logger.debug(f"Could not parse message timestamp: {e}")
            return False

    async def cleanup_expired_nonces(self):
        """Clean up expired nonces (run periodically)."""

        result = await self.db.execute("""
            DELETE FROM auth_nonces
            WHERE expires_at < datetime('now')
        """)

        if result.rowcount > 0:
            self.logger.info(f"Cleaned up {result.rowcount} expired nonces")
