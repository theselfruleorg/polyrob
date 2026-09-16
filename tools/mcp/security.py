"""
MCP Security utilities.

URL validation for SSRF protection. The Fernet credential store (``MCPEncryption`` /
``get_encryption`` + key loading) lives in ``core/security/encryption.py`` since S6
(2026-08-29) and is re-exported here for this tier's callers.
"""

import os
import json
import socket
import ipaddress
import logging
from pathlib import Path
from typing import Optional, Tuple, Dict, Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

from core.security.encryption import (  # noqa: E402,F401  (re-exported for tools-tier callers + tests)
    _KEY_FILE_PATH,
    _is_production,
    _key_file_path,
    _load_or_generate_key,
    MCPEncryption,
    get_encryption,
)


from core.security.url_policy import MCPURLValidator, get_url_validator  # noqa: E402,F401
