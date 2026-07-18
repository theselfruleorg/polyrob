"""Platform-agnostic user-related utility functions."""

from typing import Optional, Dict, Any, Tuple
import logging
import re
import json
import hashlib
import uuid

logger = logging.getLogger(__name__)


def extract_user_data(user_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Extract user data from a user dictionary.

    Args:
        user_dict: Dictionary containing user information

    Returns:
        Dict[str, Any]: Dictionary with user data
    """
    return {
        'user_id': str(user_dict.get('id', user_dict.get('user_id', ''))),
        'first_name': user_dict.get('first_name', ''),
        'last_name': user_dict.get('last_name', ''),
        'email': user_dict.get('email', ''),
        'wallet_address': user_dict.get('wallet_address', ''),
    }


def validate_email(email: str) -> Tuple[bool, str]:
    """Validate email format.

    Args:
        email: Email to validate

    Returns:
        Tuple[bool, str]: (is_valid, reason)
    """
    if not email:
        return False, "Email cannot be empty"

    # Basic validation
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if not re.match(pattern, email):
        return False, "Invalid email format"

    return True, "Valid email"


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


def is_valid_hash_id(user_id: str) -> bool:
    """Check if a string is a valid hash-based user ID.

    Args:
        user_id: String to check

    Returns:
        bool: True if string matches hash ID format
    """
    # Check if it's a 24-character hex string
    pattern = r'^[0-9a-f]{24}$'
    return bool(re.match(pattern, user_id.lower()))


def get_id_type(user_id: str) -> str:
    """Determine the type of user ID.

    Args:
        user_id: ID to check

    Returns:
        str: 'hash_id', 'wallet', 'email', or 'unknown'
    """
    if is_valid_hash_id(user_id):
        return 'hash_id'
    elif user_id.startswith('0x') and validate_wallet_address(user_id)[0]:
        return 'wallet'
    elif '@' in user_id and validate_email(user_id)[0]:
        return 'email'
    else:
        return 'unknown'


def format_user_display_name(user_data: Dict[str, Any]) -> str:
    """Format a user's display name from their data.

    Args:
        user_data: Dictionary containing user information

    Returns:
        str: Formatted display name
    """
    first_name = user_data.get('first_name', '')
    last_name = user_data.get('last_name', '')
    wallet_address = user_data.get('wallet_address', '')
    user_id = user_data.get('user_id', '')

    if first_name:
        name = first_name
        if last_name:
            name += f" {last_name}"
    elif wallet_address:
        # Show truncated wallet address
        name = f"{wallet_address[:6]}...{wallet_address[-4:]}"
    elif user_id:
        name = f"User {user_id[:8]}..."
    else:
        name = "Unknown User"

    return name