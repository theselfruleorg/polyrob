"""The single ENV-VAR-NAME predicate for DISPLAY REDACTION.

Reused by cli config show, core.config_service, and the webview config router.

NOTE: this is distinct from core/bootstrap._is_secret_key, which is a *backfill
selector* (a narrow allowlist of names we import from config/.env.production) —
a different question; do not conflate them.

Value-shape scrubbing of transcript text is yet another concern and lives in
cli/ui/secrets.py.
"""
from __future__ import annotations

_SECRET_HINTS = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "PASSPHRASE", "PASS", "JWT",
    "CREDENTIAL", "PRIVATE", "SIGNATURE", "SEED", "MNEMONIC",
)


def is_secret_key(name: str) -> bool:
    """Return True if name appears to be a secret (for display redaction).

    Checks if any of the secret substring hints appear case-insensitively in the name.
    Used to decide whether to redact a config value in display output.

    Args:
        name: The environment variable name to check.

    Returns:
        True if the name matches a secret hint, False otherwise.
    """
    if not name:
        return False
    upper = name.upper()
    return any(hint in upper for hint in _SECRET_HINTS)
