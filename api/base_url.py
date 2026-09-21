"""The ONE public base URL this instance advertises (B42).

The agent card and the ERC-8004 registration file each used to build their
absolute URLs from the request's ``Host`` header (falling back to
``X-Forwarded-Proto``). Both headers are CLIENT-SUPPLIED: anyone could fetch
``/.well-known/agent.json`` with ``Host: evil.example`` and get back a card
telling the next agent to send its tasks — and its x402 payments — to
``https://evil.example/a2a``. The ERC-8004 file is worse: its URLs can be
written into a token URI on-chain, permanently.

So the base URL is CONFIGURATION, never request-derived:

1. ``A2A_BASE_URL`` (or ``POLYROB_BASE_URL``) if set — the operator's answer.
2. otherwise the loopback base this process is actually bound to, which is
   honest for a local install and useless to an attacker.

:func:`public_base_url` returns ``None`` when nothing is configured AND the
caller asked for a genuinely PUBLIC url, so a document can OMIT a link rather
than publish a loopback address as if the world could reach it.
"""

import os
from typing import Optional

#: Env names checked, in order, for the operator-configured base URL.
BASE_URL_ENV_VARS = ("A2A_BASE_URL", "POLYROB_BASE_URL")


def _configured() -> Optional[str]:
    for name in BASE_URL_ENV_VARS:
        value = (os.environ.get(name) or "").strip().rstrip("/")
        if value:
            return value
    return None


def loopback_base_url() -> str:
    """The loopback base this process listens on (``UVICORN_PORT``).

    Spelled ``localhost`` — the same literal the agent card has always fallen
    back to — so an unconfigured instance renders exactly what it rendered
    before B42; only the request-derived branch was removed.
    """
    port = (os.environ.get("UVICORN_PORT") or "9000").strip() or "9000"
    return f"http://localhost:{port}"


def base_url(request=None) -> str:
    """The base URL to render in a response. Never reads ``Host``.

    ``request`` is accepted (and ignored) so call sites read naturally and a
    future reader cannot mistake its absence for an oversight.
    """
    return _configured() or loopback_base_url()


def public_base_url() -> Optional[str]:
    """The base URL a THIRD PARTY can reach, or ``None`` if there isn't one.

    Use this for anything published outside the process (an ERC-8004 image
    URL, a registration document). A loopback address is not a public URL, and
    emitting one as if it were is a link that can only ever 404.
    """
    return _configured()


def is_public_base_url_configured() -> bool:
    """Whether an operator pinned a public base URL."""
    return _configured() is not None
