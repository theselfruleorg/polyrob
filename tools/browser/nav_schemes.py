"""085 W1 (custody L-2): the scheme allowlist every agent-driven navigation passes FIRST.

The old guard was ``url.startswith('file://')`` — case-sensitive, so
``FILE:///…/.polyrob/.env`` passed it, and the SSRF guard after it returns early
under ``BROWSER_ALLOW_PRIVATE_URLS``. An allowlist of web schemes, compared
lower-case, cannot miss a spelling. ``data:`` stays allowed (the agent renders
generated images with it); ``about:`` covers ``about:blank``.
"""
from typing import Optional
from urllib.parse import urlparse

NAVIGABLE_SCHEMES = frozenset({"http", "https", "data", "about"})

FILE_URL_REFUSAL = (
    "Cannot open file:// URLs in browser. If you're trying to view an image, you already "
    "have vision capabilities - just analyze the image content that was passed to you in "
    "the message. Alternatively, you can use filesystem_read_file to get the image content "
    "and create an HTML page with a data URL.")


def url_scheme(url: str) -> str:
    """The lower-cased scheme of *url* ('' when none or unparsable)."""
    try:
        return (urlparse((url or "").strip()).scheme or "").lower()
    except Exception:
        return ""


def navigation_scheme_refusal(url: str) -> Optional[str]:
    """None when *url*'s scheme may be navigated; else the refusal text.

    A ``file:`` URL, or a web URL that embeds ``file:///`` (a redirect lure),
    gets the file text; any other non-web scheme the allowlist text."""
    scheme = url_scheme(url)
    if scheme == "file" or (scheme != "data" and "file:///" in (url or "").lower()):
        return FILE_URL_REFUSAL
    if scheme not in NAVIGABLE_SCHEMES:
        return (f"Cannot open this URL: the browser opens only http, https, data and about "
                f"URLs (got scheme {scheme or 'none'!r}). Give a full https:// address.")
    return None
