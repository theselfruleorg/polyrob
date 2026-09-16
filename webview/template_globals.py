"""Jinja globals for the console templates (extracted from `server.py`, 2026-09-13).

Values every server-rendered page needs without each route threading them through
its own context dict: branding, version, posture defaults, auth state, and the ONE
upload allowlist the upload endpoint enforces.

Extracted because `server.py` sits on its size ratchet
(`tests/test_file_size_ratchet.py`) and this block is a self-contained concern:
"what does a template get for free?". Registration is one call from `server.py`.
"""
from typing import Any

from core.surfaces.inbound_attachments import upload_accept_attribute
from core.version import get_version


def _request_is_authenticated(request: Any) -> bool:
    """Jinja helper: auth state straight from request.state (C4 contract).

    030 S5: layout.html shows a Logout link for the authenticated own_ops owner.
    Registered as a global (same pattern as `is_multitenant_posture`) so every
    server-rendered page gets it without threading a context var through each
    route; the lazy import matches the route-level style.
    """
    from utils.auth_utils import is_authenticated
    return is_authenticated(request)


def _console_nav() -> list:
    """The five destinations the shell renders (043 C8).

    layout.html is the shell the surviving webgate pages extend (the error page,
    /pending, status, sign-in, admin). It renders the same five destinations
    ``pages_new`` does, read from that module's own NAV table rather than a
    second copy of the list, so a page on this template is neither a dead end nor
    a wall of links to unregistered routes.
    """
    try:
        from webview.pages_new import NAV
        return [{"href": item["href"], "label": item["key"].title()} for item in NAV]
    except Exception:  # the shell is optional; a nav we cannot build is no nav
        return []


def register(templates: Any, webgate: Any) -> None:
    """Install every console-wide Jinja global on ``templates``."""
    from webview.theme import theme_preference, show_avatar
    g = templates.env.globals
    g["theme_preference"] = theme_preference
    g["show_avatar"] = show_avatar
    # UI branding (Workstream D).
    g["console_display_name"] = webgate.console_display_name
    g["branding"] = webgate.branding_config
    g["get_version"] = get_version
    # The ONE upload allowlist — the same list the /workspace/upload endpoint and
    # its MIME sniff enforce, so the file picker can never offer a type the server
    # refuses (it used to offer .doc/.docx that the endpoint rejected).
    g["upload_accept"] = upload_accept_attribute()
    # Posture defaults for the layout's tenant-nav block (P0-3): a page that does
    # not pass `is_multitenant` falls back to the posture SSOT instead of "shown".
    g["is_multitenant_posture"] = webgate.is_multitenant
    g["is_own_ops_posture"] = webgate.is_own_ops
    g["request_is_authenticated"] = _request_is_authenticated
    # 043 C8: the five destinations, decided in ONE place.
    g["console_nav"] = _console_nav
