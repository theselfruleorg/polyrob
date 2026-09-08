"""032 — screening the declared app env by KEY *and* by VALUE.

An app container's environment is exactly what the deploy request declared, so
it is the one place a host credential could be smuggled into a publicly
reachable process. Screening the key NAME alone is not enough:
``{"GREETING": "sk-proj-…"}`` is a secret with an innocent name.

The value scan is deliberately CONSERVATIVE — a false positive blocks a
legitimate deploy — so it demands a strong shape: a well-known credential
prefix, a PEM header, a 64-hex private key, or a long opaque token that is
simultaneously base64/hex-shaped, mixed-case-with-digits and high-entropy
(measured: legitimate long config values sit at ≤4.5 bits/char, random 48-char
tokens at ≥4.53). Values are NEVER echoed — only the offending key name.
"""
import collections
import math
import re
from typing import Optional

#: Mirror of ``tools/code_exec/env_policy.SECRET_PAT``. ``core`` may not import
#: ``tools`` (tests/test_layering_ratchet.py freezes the upward edges), so the
#: pattern is duplicated here and the parity is pinned by
#: tests/unit/core/app_service/test_app_registry.py.
SECRET_KEY_RE = re.compile(
    r"(API_KEY|SECRET|TOKEN|PASSWORD|PRIVATE_KEY|ACCESS_KEY|MNEMONIC|SEED|CREDENTIAL)",
    re.IGNORECASE,
)

#: ``(what it looks like, pattern)`` — each one is a shape no ordinary config
#: value has.
VALUE_SHAPES = (
    ("an OpenAI/Anthropic-style key (sk-…)", re.compile(r"sk-[A-Za-z0-9_-]{16,}")),
    ("a GitHub token (gh?_…)", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("a GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}")),
    ("a Slack token (xox…)", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("an AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a Google API key (AIza…)", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("a Telegram bot token", re.compile(r"\b\d{6,12}:AA[A-Za-z0-9_-]{30,}")),
    ("a PEM private key block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("a 64-hex private key", re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}\b")),
    ("a JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
)

_OPAQUE_RE = re.compile(r"[A-Za-z0-9+/=_-]{48,}")
_OPAQUE_MIN_LEN = 48
_OPAQUE_MIN_ENTROPY = 4.6


def _entropy(value: str) -> float:
    counts = collections.Counter(value)
    n = len(value)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _looks_opaque_token(value: str) -> bool:
    """A long base64/hex-shaped blob with mixed case, digits and token-grade
    entropy. Length alone is never enough."""
    v = value.strip()
    if len(v) < _OPAQUE_MIN_LEN or not _OPAQUE_RE.fullmatch(v):
        return False
    if not (any(c.islower() for c in v) and any(c.isupper() for c in v)
            and any(c.isdigit() for c in v)):
        return False
    return _entropy(v) >= _OPAQUE_MIN_ENTROPY


def secret_value_reason(value: str) -> Optional[str]:
    """Name the credential shape *value* carries, or ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    for what, pattern in VALUE_SHAPES:
        if pattern.search(value):
            return what
    if _looks_opaque_token(value):
        return "a long high-entropy opaque token"
    return None
