"""P3 — branding is polyrob/rob, with the old/test/placeholder strings removed.

The webgate templates must not carry the old framework name (`AutoV2`/`Auto Agent`),
the test bot handle (`@testestovichbot`/`testesto…`), or any placeholder-bot string.
Framework = ``polyrob``; instance persona = ``rob``/``Rob`` (kept). Instance
domain/support-handle copy is env-driven via ``branding_config()`` (generic
defaults), so it is not scanned here.
"""
import re
from pathlib import Path

import pytest

_TEMPLATES = Path(__file__).resolve().parents[3] / "webview" / "templates"

# Case-insensitive substrings that must NOT appear anywhere in templates.
_FORBIDDEN = [
    "testesto",      # @testestovichbot test bot
    "autov2",        # old framework name
    "auto agent",    # old framework UI string
]


def _template_files():
    return sorted(_TEMPLATES.rglob("*.html"))


def test_templates_exist():
    assert _template_files(), f"no templates under {_TEMPLATES}"


@pytest.mark.parametrize("needle", _FORBIDDEN)
def test_no_forbidden_branding_string(needle):
    offenders = []
    for f in _template_files():
        text = f.read_text(encoding="utf-8", errors="ignore").lower()
        if needle in text:
            offenders.append(str(f))
    assert not offenders, f"forbidden branding {needle!r} found in: {offenders}"


def test_layout_title_calls_console_display_name():
    layout = (_TEMPLATES / "layout.html").read_text(encoding="utf-8", errors="ignore")
    # The default <title>/header brand is now sourced from console_display_name()
    # (registered as a Jinja global in server.py/pages.py) rather than a static
    # literal, so an instance can override it via POLYROB_CONSOLE_NAME.
    assert re.search(r"<title>\{%\s*block title\s*%\}\{\{\s*console_display_name\(\)\s*\}\}", layout)
    assert "console_display_name()" in layout


def test_no_placeholder_href_hash():
    offenders = []
    for f in _template_files():
        text = f.read_text(encoding="utf-8", errors="ignore")
        if 'href="#"' in text:
            offenders.append(str(f))
    assert not offenders, f'placeholder href="#" found in: {offenders}'


def test_no_stale_hardcoded_version_string():
    # "1.0.0" must not appear as a literal brand-version string anymore — the
    # real version is rendered via get_version() everywhere it used to be
    # hardcoded. (The qrcodejs CDN pin that needed an exclusion here lived in
    # profile.html, deleted 2026-09-21 with its posture row — the page drove
    # `/api/payments/*`, a router this app never mounts, so every panel on it
    # answered 404.)
    offenders = []
    for f in _template_files():
        text = f.read_text(encoding="utf-8", errors="ignore")
        if "1.0.0" in text:
            offenders.append(str(f))
    assert not offenders, f"stale hardcoded version found in: {offenders}"
