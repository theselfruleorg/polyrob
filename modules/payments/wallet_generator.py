"""Deposit wallet generator for deterministic user deposit addresses."""

import hashlib
import logging
from eth_account import Account

logger = logging.getLogger(__name__)


class DepositWalletGenerator:
    """Generate deterministic deposit addresses for users."""

    def __init__(self, master_seed: str = None):
        """
        Initialize with master seed.

        Args:
            master_seed: Master seed for deterministic key generation.
                        ⚠️ CRITICAL: Must be kept secret and backed up!
                        If lost, cannot sweep funds from deposit addresses.

        Raises:
            ValueError: If master_seed is invalid
        """

        if master_seed and len(master_seed) < 32:
            raise ValueError("Invalid master seed - must be 32+ chars")

        self.master_seed = master_seed
        self.logger = logging.getLogger('payments.wallet_generator')

        if master_seed:
            self.logger.info("Wallet generator initialized with master seed")
        else:
            self.logger.warning("Wallet generator initialized WITHOUT master seed - generation disabled")

    def generate_deposit_address(self, user_id: str) -> str:
        """
        Generate deterministic deposit address for user.

        Same user_id always generates same address.
        This allows us to regenerate private keys for sweeping.

        Args:
            user_id: User ID

        Returns:
            Ethereum address

        Raises:
            ValueError: If master seed not configured
        """

        if not self.master_seed:
            raise ValueError("Master seed not configured - cannot generate deposit addresses")

        # Derive private key from master seed + user_id
        key_material = hashlib.pbkdf2_hmac(
            'sha256',
            self.master_seed.encode('utf-8'),
            user_id.encode('utf-8'),
            iterations=100000,
            dklen=32
        )

        # Create Ethereum account
        account = Account.from_key(key_material)

        self.logger.info(f"Generated deposit address for {user_id}: {account.address}")

        return account.address

    def get_account_for_sweep(self, user_id: str) -> Account:
        """
        Regenerate account for sweeping (has private key).

        Args:
            user_id: User ID

        Returns:
            Account object with private key

        Raises:
            ValueError: If master seed not configured
        """

        if not self.master_seed:
            raise ValueError("Master seed not configured - cannot generate accounts")

        key_material = hashlib.pbkdf2_hmac(
            'sha256',
            self.master_seed.encode('utf-8'),
            user_id.encode('utf-8'),
            iterations=100000,
            dklen=32
        )

        return Account.from_key(key_material)

    def get_private_key_for_address(self, address: str) -> bytes:
        """Get private key for a deposit address.

        Note: This requires looking up the user_id from the address first.
        For efficiency, treasury sweeper should pass user_id directly.

        Args:
            address: Ethereum address

        Returns:
            Private key bytes

        Raises:
            ValueError: If address not found or master seed not configured
        """
        # This is a simplified version - in production you'd need to
        # query the database to find which user_id generated this address
        # For now, we'll raise an error and require using get_account_for_user_id instead
        raise NotImplementedError(
            "Use get_private_key_for_user_id() instead - requires user_id lookup from database"
        )

    def get_private_key_for_user_id(self, user_id: str) -> bytes:
        """Get private key for a user's deposit address.

        Args:
            user_id: User ID

        Returns:
            Private key bytes

        Raises:
            ValueError: If master seed not configured
        """
        if not self.master_seed:
            raise ValueError("Master seed not configured")

        key_material = hashlib.pbkdf2_hmac(
            'sha256',
            self.master_seed.encode('utf-8'),
            user_id.encode('utf-8'),
            iterations=100000,
            dklen=32
        )

        return key_material
