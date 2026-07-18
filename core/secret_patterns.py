"""Shared high-confidence credential-shape regexes (P4 finalization).

These six patterns were defined IDENTICALLY (byte-for-byte) in both
``core/secret_scrub.py`` (persisted-content scrub, conservative subset) and
``cli/ui/secrets.py`` (display scrub, full set). They drifted once already — the
``KV_RE`` fix for ``<PREFIX>_API_KEY=`` was made in the CLI twin but not backported
to core, silently leaking that shape from persisted message history. Defining them
in ONE place makes that class of divergence impossible: both scrubbers import from
here and layer their own extra patterns on top.

Pure module — regex objects only, no I/O.
"""
import re

#: Replacement marker (the ``<secret>…</secret>`` shape the history filter uses).
REDACTED = "<secret>redacted</secret>"

#: PEM private-key blocks (multi-line) — redact the whole block.
PEM_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

#: ``Bearer <token>`` — run before the kv rule (the kv value would stop at the
#: space and leave the token). 8+ token chars.
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}")

#: ``key = value`` / ``key: value`` where the key NAME signals a credential.
#: (?<![A-Za-z0-9]) + prefix-capture instead of a leading \b so that
#: ``<PREFIX>_API_KEY=`` (the most common real env-var shape) matches — a `\b`
#: fails there because `_` is a word char. Key preserved; value (>=6 chars) redacted.
KV_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"((?:[A-Za-z0-9]+[_-])*"
    r"(?:api[_-]?key|apikey|secret|client_secret|password|passwd|"
    r"access[_-]?token|auth[_-]?token|token|authorization|bearer))"
    r"(\s*[=:]\s*)"
    r"(['\"]?)([^\s'\"]{6,})\3"
)

#: Provider-style opaque keys: ``sk-``/``pk-``/``rk-`` (OpenAI/Anthropic/Stripe).
PROVIDER_KEY_RE = re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}")

#: POLYROB API keys (``rob_…``).
POLYROB_KEY_RE = re.compile(r"\brob_[A-Za-z0-9]{16,}")

#: AWS access-key id (``AKIA…``).
AWS_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
