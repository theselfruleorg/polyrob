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

PUBLIC FACTS ARE NOT SECRETS (C1, 2026-09-21). The two length-based catch-alls
(hex / base64) used to eat every wallet address, transaction hash, UUID and
filesystem path the REPL printed — so the one line a launch verb emits
(``token: 0x…``) and the path of the file the agent just wrote both rendered as
``«redacted»``, and the owner had to go to a third-party indexer to read back
what his own agent had done. Those two rules now consult
:func:`_is_public_identifier` / :func:`_path_shaped`. The residual, stated
rather than implied: an opaque credential that is EXACTLY 43-44 base58
characters, or exactly 64 hex after ``0x``, is indistinguishable from a Solana
pubkey / a transaction hash by shape alone and would survive the catch-all. The
named-shape rules (PEM, Bearer, KV, ``sk-``, ``rob_``, AWS, JWT, opaque-token,
Google, Slack, GitHub) all run FIRST and catch the credentials that actually
exist; this gap was taken deliberately against losing every address the agent
writes down.

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
    PUBLIC_ADDRESS_RE,
    apply_ssot_shapes,
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
#:
#: ``/`` is DELIBERATELY absent from ``_B64_RE`` (C1, 2026-09-21). With it in the
#: class, ``/Users/me/.polyrob/data/sessions/abcdef`` is one 40+ char "base64"
#: token and the whole path redacted — so every workspace path, every session
#: dir and every log location the REPL printed came out as «redacted». A real
#: base64 blob is still caught segment-by-segment (its own run is >=40 chars).
_HEX_RE = re.compile(r"\b[A-Fa-f0-9]{32,}\b")
_B64_RE = re.compile(r"\b[A-Za-z0-9+]{40,}={0,2}\b")

#: An EVM transaction hash / block hash: ``0x`` + EXACTLY 64 hex. A PUBLIC fact
#: (it is what a receipt IS), and the one string that proves a money verb did
#: what it said. Anchored, so a longer hex run never matches.
_TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

#: A canonical UUID (``8-4-4-4-12``) or its undashed 32-hex form — tool-call
#: ids, session ids, request ids. An identifier, never a credential.
_UUID_RE = re.compile(
    r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32})$"
)

#: A Solana-shaped base58 public key. Wider than ``PUBLIC_ADDRESS_RE``'s 43-44
#: (a program-derived address, a mint, a signature prefix) because the catch-all
#: this exempts only fires at 40+ characters anyway.
_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _is_public_identifier(token: str) -> bool:
    """True when *token* is a PUBLIC identifier, not a credential shape.

    The invariant: a public address, a transaction hash and a UUID are FACTS the
    owner needs on screen — the launch verb's own success line is ``token: 0x…``
    — and the length-based catch-alls below cannot tell them from a secret. Each
    rule is anchored and exact-length, so widening one never exempts a private
    key (EVM private keys are ``0x`` + 64 hex too, but they are never written
    under a bare key the KV battery above has not already claimed; the residual
    is stated in the module doc).
    """
    return bool(
        PUBLIC_ADDRESS_RE.match(token)
        or _TX_HASH_RE.match(token)
        or _UUID_RE.match(token)
        or _BASE58_RE.match(token)
    )


def _path_shaped(text: str, start: int, end: int) -> bool:
    """True when the match sits inside a filesystem path / dotted identifier.

    ``/`` is gone from ``_B64_RE`` so a path is now split into segments; a
    segment is still long enough to trip the catch-all on its own. Looking at
    the ONE character on each side is what distinguishes
    ``…/sessions/<long-segment>/feed`` from a bare token on a line of its own.
    """
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    if before in ("/", "\\", "."):
        return True
    if after in ("/", "\\"):
        return True
    # ``name.ext`` / ``pkg.module`` — a dot followed by more identifier.
    return after == "." and end + 1 < len(text) and text[end + 1].isalnum()


def _catch_all_sub(pattern: "re.Pattern", text: str) -> str:
    """Apply a length-based catch-all, skipping public identifiers + paths."""
    def _repl(match: "re.Match") -> str:
        token = match.group(0)
        if _is_public_identifier(token):
            return token
        if "/" in token or "\\" in token:
            return token
        if _path_shaped(text, match.start(), match.end()):
            return token
        return REDACTED
    return pattern.sub(_repl, text)


def scrub_secrets(text: Optional[str]) -> str:
    """Redact common secret token shapes from *text* for display.

    Returns "" for None. No-op for content with no secret-shaped substrings, so
    it is safe to apply to every tool arg + result preview.
    """
    if not text:
        return ""
    # Shared ordered battery (PEM → Bearer → KV → provider → rob → AWS → JWT) —
    # ONE home in core/secret_patterns.apply_ssot_shapes so the three scrubbers
    # can't drift. Display-only extras + catch-alls layer AFTER it (JWT before
    # hex/base64 so a three-segment token redacts as one unit).
    out = apply_ssot_shapes(text, REDACTED)
    out = _GOOGLE_RE.sub(REDACTED, out)
    out = _SLACK_RE.sub(REDACTED, out)
    out = _GITHUB_RE.sub(REDACTED, out)
    out = _GITHUB_PAT_RE.sub(REDACTED, out)
    # The two length-based catch-alls are the ONLY rules here that match on
    # length rather than on a credential shape, so they are the only ones that
    # can eat a public fact. Both run through the exemption gate.
    out = _catch_all_sub(_HEX_RE, out)
    out = _catch_all_sub(_B64_RE, out)
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
