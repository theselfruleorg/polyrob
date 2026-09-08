"""Platform-agnostic user-related utility functions."""

from typing import Optional, Dict, Any, Tuple
import logging
import re
import json
import hashlib
import uuid

logger = logging.getLogger(__name__)


def validate_wallet_address(address: str) -> Tuple[bool, str]:
    """Validate Ethereum wallet address format.

    Args:
        address: Wallet address to validate

    Returns:
        Tuple[bool, str]: (is_valid, reason)
    """
    if not address:
        return False, "Wallet address cannot be empty"

    # Basic Ethereum address validation (0x followed by 40 hex chars)
    pattern = r'^0x[a-fA-F0-9]{40}$'
    if not re.match(pattern, address):
        return False, "Invalid wallet address format"

    return True, "Valid wallet address"


def generate_user_id(seed=None) -> str:
    """Generate a unique user ID hash.

    Args:
        seed: Optional seed value to use in hash generation

    Returns:
        str: Hash-based user ID
    """
    # Generate a unique ID based on UUID and optional seed
    unique_id = str(uuid.uuid4())
    if seed:
        unique_id = f"{seed}:{unique_id}"

    # Create hash
    hash_obj = hashlib.sha256(unique_id.encode())
    user_id = hash_obj.hexdigest()[:24]  # Use first 24 chars of hash

    return user_id


