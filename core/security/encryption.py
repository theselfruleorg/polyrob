"""Fernet credential store (tier-0 security primitive).

Encrypts stored third-party secrets (MCP server credentials, OAuth tokens, X session
cookies, exchange API keys) at rest. Key resolution: ``MCP_ENCRYPTION_KEY`` env (always
preferred; REQUIRED in production) → persisted dev key file → generated + persisted (dev
only). Relocated from ``tools/mcp/security.py`` in S6 (2026-08-29): three
``modules/database`` handlers imported it upward, and it never depended on MCP. The
tools module re-exports these names for its own callers; the class keeps its historical
name for the stored-data/compat surface.
"""
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# Path for persisted encryption key (development fallback).
# Anchored to the install/repo root (this file is tools/mcp/security.py ->
# parents[2]), NOT the process CWD: a relative path would regenerate the dev
# Fernet key whenever CWD changed, orphaning previously-encrypted MCP creds.
def _key_file_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / ".mcp_encryption_key"
_KEY_FILE_PATH = _key_file_path()
def _is_production() -> bool:
    """Check if running in production mode."""
    env = os.getenv('POLYROB_ENV', os.getenv('ENVIRONMENT', 'development')).lower()
    return env in ('production', 'prod')
def _load_or_generate_key() -> str:
    """
    Load encryption key from environment, file, or generate new one.
    
    Priority:
    1. MCP_ENCRYPTION_KEY environment variable (always preferred)
    2. Persisted key file (development fallback)
    3. Generate new key (development only, persisted to file)
    
    In production, FAILS HARD if no key is configured.
    
    Returns:
        Fernet key as string
        
    Raises:
        RuntimeError: In production if MCP_ENCRYPTION_KEY is not set
    """
    from cryptography.fernet import Fernet
    
    # 1. Try environment variable first
    key_str = os.getenv('MCP_ENCRYPTION_KEY')
    if key_str:
        logger.debug("Using MCP_ENCRYPTION_KEY from environment")
        return key_str
    
    # 2. In production, fail hard - no fallbacks allowed
    if _is_production():
        raise RuntimeError(
            "CRITICAL: MCP_ENCRYPTION_KEY environment variable is required in production! "
            "Generate a key with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    
    # 3. Development: Try to load from persisted file
    if _KEY_FILE_PATH.exists():
        try:
            key_str = _KEY_FILE_PATH.read_text().strip()
            if key_str:
                logger.info(f"Loaded MCP encryption key from {_KEY_FILE_PATH}")
                # Also set in environment so it's consistent for this session
                os.environ['MCP_ENCRYPTION_KEY'] = key_str
                return key_str
        except Exception as e:
            logger.warning(f"Failed to read key file {_KEY_FILE_PATH}: {e}")
    
    # 4. Development: Generate new key and persist it
    logger.warning(
        "MCP_ENCRYPTION_KEY not set - generating and persisting key for development. "
        "This key will be saved to data/.mcp_encryption_key and reused across restarts. "
        "In production, set MCP_ENCRYPTION_KEY environment variable!"
    )
    
    key_str = Fernet.generate_key().decode()
    
    # Persist to file for reuse
    try:
        _KEY_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _KEY_FILE_PATH.write_text(key_str)
        # Set restrictive permissions (owner read/write only)
        _KEY_FILE_PATH.chmod(0o600)
        logger.info(f"Persisted MCP encryption key to {_KEY_FILE_PATH}")
    except Exception as e:
        logger.warning(f"Failed to persist key to {_KEY_FILE_PATH}: {e}")
    
    # Set in environment for this session
    os.environ['MCP_ENCRYPTION_KEY'] = key_str
    
    return key_str
class MCPEncryption:
    """Encrypt/decrypt sensitive MCP data using Fernet (AES-128-CBC)."""

    def __init__(self, key: Optional[bytes] = None):
        """
        Initialize encryption with a key.

        Args:
            key: Fernet key (32 url-safe base64-encoded bytes).
                 If not provided, loads from MCP_ENCRYPTION_KEY env var or persisted file.
                 
        Raises:
            RuntimeError: In production if MCP_ENCRYPTION_KEY is not set
        """
        # Lazy import to avoid dependency issues if cryptography not installed
        from cryptography.fernet import Fernet

        if key:
            self.fernet = Fernet(key)
        else:
            # Load key using proper priority chain
            key_str = _load_or_generate_key()
            self.fernet = Fernet(key_str.encode() if isinstance(key_str, str) else key_str)

    def encrypt(self, data: str) -> bytes:
        """
        Encrypt a string.

        Args:
            data: Plain text string to encrypt

        Returns:
            Encrypted bytes
        """
        if not data:
            return b''
        return self.fernet.encrypt(data.encode())

    def decrypt(self, encrypted: bytes) -> str:
        """
        Decrypt to string.

        Args:
            encrypted: Encrypted bytes

        Returns:
            Decrypted string
        """
        if not encrypted:
            return ''
        return self.fernet.decrypt(encrypted).decode()

    def encrypt_dict(self, data: Dict[str, Any]) -> bytes:
        """
        Encrypt a dictionary as JSON.

        Args:
            data: Dictionary to encrypt

        Returns:
            Encrypted bytes
        """
        if not data:
            return b''
        return self.encrypt(json.dumps(data))

    def decrypt_dict(self, encrypted: bytes) -> Dict[str, Any]:
        """
        Decrypt to dictionary.

        Args:
            encrypted: Encrypted bytes

        Returns:
            Decrypted dictionary
        """
        if not encrypted:
            return {}
        return json.loads(self.decrypt(encrypted))

    @staticmethod
    def generate_key() -> bytes:
        """
        Generate a new encryption key.

        Returns:
            New Fernet key (url-safe base64-encoded)
        """
        from cryptography.fernet import Fernet
        return Fernet.generate_key()
def get_encryption() -> MCPEncryption:
    """Get singleton encryption instance."""
    global _encryption_instance
    if '_encryption_instance' not in globals():
        _encryption_instance = MCPEncryption()
    return _encryption_instance
