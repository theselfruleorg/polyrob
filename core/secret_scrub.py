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

# P4 finalization: the high-confidence credential-shape patterns + the REDACTED
# marker live in ONE place (core.secret_patterns) so this persisted-content scrubber
# and the cli/ui/secrets.py display scrubber can NEVER diverge again (the _KV_RE fix
# that leaked <PREFIX>_API_KEY= was a divergence bug). This module layers no extra
# patterns — it deliberately omits the aggressive hex/base64 catch-alls (see docstring).
from core.secret_patterns import (  # noqa: E402
    REDACTED,
    PEM_RE as _PEM_RE,
    BEARER_RE as _BEARER_RE,
    KV_RE as _KV_RE,
    PROVIDER_KEY_RE as _PROVIDER_KEY_RE,
    POLYROB_KEY_RE as _ROB_KEY_RE,
    AWS_RE as _AWS_RE,
)


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
