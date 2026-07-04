"""core.secret_scrub — conservative secret-shape redaction for PERSISTED content.

POLYROB's message-history sensitive-data filter (``messages/filters.py``) was
allowlist-only: it redacted just the credentials explicitly registered in
``sensitive_data``. Any UNregistered secret that lands in a tool result (a
``cat .env``, an MCP response echoing an auth header, a printed private key) was
written verbatim into ``message_history.json`` and the compaction checkpoints on
disk, in the clear.

This module adds a pattern backstop. Unlike the CLI's display-only scrubber
(``cli/ui/secrets.py``), this one runs over content that is FED BACK TO THE MODEL
and kept as working state, so it deliberately OMITS the aggressive
hex/base64 catch-alls: redacting every 32-char hex or 40-char base64 run would
corrupt legitimate working data (a git SHA the agent is comparing, a base64 blob
it is processing). Only HIGH-CONFIDENCE credential shapes are redacted here.

Pure (no I/O, no state). Defense-in-depth, NOT a security boundary — the real fix
for secret EXPOSURE is workspace path-confinement at the tool layer.
"""
from __future__ import annotations

import re
from typing import Optional

#: Replacement marker. Matches the ``<secret>…</secret>`` convention the history
#: filter already uses for registered sensitive values, so the model sees one shape.
REDACTED = "<secret>redacted</secret>"

#: PEM private-key blocks (multi-line) — redact the whole block.
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

#: ``Bearer <token>`` — run before the kv rule (kv value would stop at the space
#: and leave the token). 8+ token chars.
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}")

#: ``key = value`` / ``key: value`` where the key NAME signals a credential.
#: Key is preserved (line stays readable); only the value (>=6 chars) is redacted.
_KV_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|secret|client_secret|password|passwd|"
    r"access[_-]?token|auth[_-]?token|token|authorization|bearer)"
    r"(\s*[=:]\s*)"
    r"(['\"]?)([^\s'\"]{6,})\3"
)

#: Provider-style opaque keys: ``sk-``/``pk-``/``rk-`` (OpenAI/Anthropic/Stripe),
#: ``rob_`` (POLYROB API keys), and AWS ``AKIA…`` access-key ids.
_PROVIDER_KEY_RE = re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}")
_ROB_KEY_RE = re.compile(r"\brob_[A-Za-z0-9]{16,}")
_AWS_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")


def scrub_secret_shapes(text: Optional[str]) -> str:
    """Redact high-confidence credential shapes from *text*.

    Returns "" for None. No-op for content with no secret-shaped substrings, so it
    is safe to apply to every persisted message's string content and tool args.
    Conservative by design — see the module docstring on why hex/base64 blobs are
    intentionally left intact.
    """
    if not text:
        return ""
    out = _PEM_RE.sub(REDACTED, text)
    out = _BEARER_RE.sub(REDACTED, out)
    out = _KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    out = _PROVIDER_KEY_RE.sub(REDACTED, out)
    out = _ROB_KEY_RE.sub(REDACTED, out)
    out = _AWS_RE.sub(REDACTED, out)
    return out
