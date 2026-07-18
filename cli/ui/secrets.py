"""secrets.py — display-only secret scrubbing for the CLI tool transcript.

When the CLI renders tool calls + results BY DEFAULT (not just under /verbose),
the args and result previews flow into the terminal scrollback — which persists,
gets pasted into bug reports, and is screen-shared. A ``read_file`` of an ``.env``,
a ``cat ~/.aws/credentials``, or an MCP result echoing an auth header can put a
LIVE secret there. This module redacts the high-value secret shapes before they
are rendered.

IMPORTANT: this is a best-effort DISPLAY backstop, NOT a security boundary. A
regex scrubber has false negatives (novel/short token shapes, secrets embedded in
structured blobs) — the real fix for secret EXPOSURE is workspace path-confinement
at the tool layer. Treat this as defense-in-depth for the terminal surface only.

Everything here is PURE (no I/O, no state) and trivially unit-testable.
"""

from __future__ import annotations

import re
from typing import Optional

#: The marker substituted in place of a detected secret.
REDACTED = "«redacted»"

# ---------------------------------------------------------------------------
# Patterns (ordered: structural blocks first, then key=value, then bare tokens)
# ---------------------------------------------------------------------------

# P4 finalization: the six high-confidence credential-shape patterns are shared with
# core/secret_scrub.py via core.secret_patterns so the two scrubbers can't diverge (the
# _KV_RE <PREFIX>_API_KEY= fix was a divergence bug). NOTE: this display scrubber keeps
# its OWN REDACTED marker ("«redacted»", above) — only the regexes are shared. The
# cli-only patterns (Google/Slack/GitHub/hex/base64 catch-alls) stay local below.
from core.secret_patterns import (  # noqa: E402
    PEM_RE as _PEM_RE,
    BEARER_RE as _BEARER_RE,
    KV_RE as _KV_RE,
    PROVIDER_KEY_RE as _PROVIDER_KEY_RE,
    POLYROB_KEY_RE as _ROB_KEY_RE,
    AWS_RE as _AWS_RE,
)

#: Google API key (e.g. Gemini ``AIza…`` — 39 chars, under the base64 rule).
_GOOGLE_RE = re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")

#: Slack tokens (``xoxb-``/``xoxp-``/``xoxa-``/``xoxr-``/``xoxs-``).
_SLACK_RE = re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}")

#: GitHub tokens: fine-grained (``ghp_``/``gho_``/``ghu_``/``ghs_``/``ghr_``) + PAT.
_GITHUB_RE = re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")
_GITHUB_PAT_RE = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}")

# (_BEARER_RE is imported from core.secret_patterns above — shared with the
# persisted-content scrubber so it can't diverge. P4 finalization.)

#: Long opaque hex / base64 blobs (>=40 base64 chars, >=32 hex chars). Catches
#: hashes/JWT-ish blobs; a redacted git SHA is harmless collateral.
_HEX_RE = re.compile(r"\b[A-Fa-f0-9]{32,}\b")
_B64_RE = re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")


def scrub_secrets(text: Optional[str]) -> str:
    """Redact common secret token shapes from *text* for display.

    Returns "" for None. No-op for content with no secret-shaped substrings, so
    it is safe to apply to every tool arg + result preview.
    """
    if not text:
        return ""
    out = _PEM_RE.sub(REDACTED, text)
    # Bearer BEFORE the kv rule: ``Authorization: Bearer <jwt>`` — the kv value
    # stops at the first space (it would redact only "Bearer", leaving the token).
    out = _BEARER_RE.sub(REDACTED, out)
    out = _KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    out = _PROVIDER_KEY_RE.sub(REDACTED, out)
    out = _ROB_KEY_RE.sub(REDACTED, out)
    out = _AWS_RE.sub(REDACTED, out)
    out = _GOOGLE_RE.sub(REDACTED, out)
    out = _SLACK_RE.sub(REDACTED, out)
    out = _GITHUB_RE.sub(REDACTED, out)
    out = _GITHUB_PAT_RE.sub(REDACTED, out)
    out = _HEX_RE.sub(REDACTED, out)
    out = _B64_RE.sub(REDACTED, out)
    return out


def scrub_then_cap(text: Optional[str], *, limit: int = 200) -> str:
    """Scrub secrets FIRST, then cap length with an ellipsis.

    Order matters: capping before scrubbing could leave a secret half-shown (the
    cut happening mid-token would defeat the redaction). Returns "" for None.
    """
    flat = scrub_secrets(text)
    flat = " ".join(flat.split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"
