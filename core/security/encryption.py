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
#
# It lives in the DATA HOME (``core.runtime_paths.resolve_data_home``) beside the
# stores it protects — never in the install tree, which for a pip install is
# ``site-packages`` (shared, and replaced on upgrade). The historical install-root
# location is still READ once and migrated, so previously-encrypted credentials
# stay decryptable.
_KEY_FILENAME = ".mcp_encryption_key"


def _key_file_path() -> Path:
    from core.runtime_paths import resolve_data_home
    return Path(resolve_data_home()).resolve() / _KEY_FILENAME


def _legacy_key_file_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / _KEY_FILENAME


_KEY_FILE_PATH = _legacy_key_file_path()  # re-exported name; the legacy location
#: The dev key, once resolved, for the life of the process — kept HERE rather
#: than copied into ``os.environ``, where every child process would inherit it.
_DEV_KEYS: Dict[str, str] = {}


def _is_production() -> bool:
    """Check if running in production mode."""
    env = os.getenv('POLYROB_ENV', os.getenv('ENVIRONMENT', 'development')).lower()
    return env in ('production', 'prod')


def _read_key(path: Path) -> Optional[str]:
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        return os.read(fd, 4096).decode("ascii", errors="strict").strip() or None
    finally:
        os.close(fd)


def _persist_new_key(path: Path, key_str: str) -> str:
    """Create *path* O_EXCL at 0600; if another process won the race, return ITS key."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        other = _read_key(path)
        if not other:
            raise RuntimeError(f"MCP encryption key file {path} exists but is empty")
        return other
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, key_str.encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return key_str


def _load_or_generate_key() -> str:
    """
    Load encryption key from environment, file, or generate new one.

    Priority:
    1. MCP_ENCRYPTION_KEY environment variable (always preferred)
    2. Persisted key file in the data home (development fallback), migrating
       the legacy install-tree key file when that is all there is
    3. Generate new key (development only, persisted O_EXCL at 0600)

    In production, FAILS HARD if no key is configured. An existing key file
    that cannot be read is an error too — a fresh key would orphan every
    credential encrypted with the old one.
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

    path = _key_file_path()
    if str(path) in _DEV_KEYS:
        return _DEV_KEYS[str(path)]
    try:
        key_str = _read_key(path)
    except OSError as e:
        raise RuntimeError(f"MCP encryption key file {path} is unreadable ({e}); refusing to "
                           f"generate a new key that would orphan stored credentials") from e
    if key_str:
        logger.info("Loaded MCP encryption key from %s", path)
        _DEV_KEYS[str(path)] = key_str
        return key_str

    # 3. Migrate the legacy install-tree key, else generate a fresh one.
    legacy = _legacy_key_file_path()
    legacy_key = None
    if legacy != path:
        try:
            legacy_key = _read_key(legacy)
        except OSError as e:
            logger.warning("Legacy MCP key file %s unreadable: %s", legacy, e)
    if legacy_key:
        logger.info("Migrating the MCP encryption key from %s to %s", legacy, path)
        key_str = legacy_key
    else:
        logger.warning(
            "MCP_ENCRYPTION_KEY not set - generating and persisting a development key at %s. "
            "In production, set the MCP_ENCRYPTION_KEY environment variable!", path)
        key_str = Fernet.generate_key().decode()
    try:
        key_str = _persist_new_key(path, key_str)
    except OSError as e:
        logger.warning("Failed to persist the MCP key to %s: %s", path, e)
    _DEV_KEYS[str(path)] = key_str
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
