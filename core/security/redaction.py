"""Redaction for values that carry a credential in a URL or an error string.

A provider endpoint puts its API key in the PATH
(``https://<net>.g.alchemy.com/v2/<api-key>``) or the query, where every
name-keyed secret scrubber misses it — the scrubbers look for `KEY=value`, and
this is just a URL. That is not hypothetical: the x402 settlement scan's
"detection active" line published a real Alchemy key to the production journal.

That site was fixed with a private `_redact_rpc`; a SECOND site
(`tools/defi/providers/alchemy_index.py`) composed the same URL and logged its
failures with `exc_info=True`. This module is the shared home so the next site
imports the fix instead of re-deriving it — or missing it.
"""
from __future__ import annotations

from urllib.parse import urlsplit

#: Below this length a "secret" is more likely a placeholder than a credential,
#: and scrubbing it would blank ordinary words out of a message.
_MIN_SECRET_LEN = 8


def redact_url(url: str) -> str:
    """An endpoint safe to log — scheme + host (+ port), never the credential.

    Any path or query beyond ``/`` is credential-shaped: it is MARKED (``/…``)
    rather than shown, so a reader can still tell a keyed endpoint from a bare
    public one.
    """
    try:
        parts = urlsplit(url)
        if not parts.scheme or not parts.hostname:
            return "<rpc>" if url else ""
        base = f"{parts.scheme}://{parts.hostname}"
        if parts.port:
            base += f":{parts.port}"
        if (parts.path and parts.path != "/") or parts.query:
            base += "/…"
        return base
    except Exception:
        return "<rpc>"


def scrub_secret(text: str, *secrets: str) -> str:
    """*text* with every known *secret* replaced by ``<redacted>``.

    For the case `redact_url` cannot reach: an exception message that embedded
    the full URL itself (``httpx.HTTPStatusError`` renders
    ``"... for url 'https://host/v2/<key>'"``). Scrubbing the value we already
    hold is the durable fix — it survives a future `raise_for_status()`, a
    different client library, or a chained exception.
    """
    out = str(text)
    for secret in secrets:
        secret = (secret or "").strip()
        if len(secret) >= _MIN_SECRET_LEN:
            out = out.replace(secret, "<redacted>")
    return out


def fingerprint(value, keep: int = 4) -> str:
    """A display-safe fingerprint of a credential — THE only printable form.

    Enough for an owner to tell two credentials apart ("is that the key I just
    pasted?") without printing one. Short values redact whole: for anything
    under ``3 * keep`` a prefix+suffix would reveal most of it. ``polyrob auth
    status``, ``polyrob config show`` and the LLM auth resolver all render
    through here; two of them used to carry their own reveal sizes.
    """
    if not value:
        return "(none)"
    text = str(value)
    if len(text) <= keep * 3:
        return "*" * 8
    return f"{text[:keep]}…{text[-keep:]}"
