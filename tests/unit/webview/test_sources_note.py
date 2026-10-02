"""070 W0.1 — no console page carries a developer sources footer.

043 R11 put a ``<p class="sources">`` under every screen: a paragraph of
function names (``build_recap``, ``core.surfaces.inbox``, ``get_workspace_dir``)
that said where each number came from. That traceability was for the builder,
not the owner — on a phone the chat footer ran under the bottom nav, and every
page ended in code names.

The footers are gone on purpose. The data source of each block is listed per
page in ``webview/README.md``; the Inbox keeps one plain line that says whether
every list answered (``inbox.checked_all`` / ``inbox.checked_some``). This test
holds the count at zero.
"""
import re
from pathlib import Path

from webview.copy import STRINGS

_REPO = Path(__file__).resolve().parents[3]
_TEMPLATES = _REPO / "webview" / "templates"

_SOURCES_P = re.compile(r'<p\s+class="sources"', re.I)


def _shell_templates():
    out = []
    for path in sorted(_TEMPLATES.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        if 'extends "shell.html"' in text:
            out.append(path)
    return out


def test_no_shell_screen_carries_a_sources_note():
    found = [p.name for p in _shell_templates()
             if _SOURCES_P.search(p.read_text(encoding="utf-8"))]
    assert not found, f"a developer sources footer is back in: {found}"


def test_no_copy_key_is_a_sources_note():
    keys = sorted(k for k in STRINGS
                  if k.endswith(".sources") or k.endswith(".sources.cites")
                  or ".sources." in k)
    assert not keys, f"sources-note copy keys are back: {keys}"


def test_the_scan_found_the_new_screens():
    """A ratchet over an empty set is a ratchet nobody has seen work."""
    names = {p.name for p in _shell_templates()}
    assert names >= {"inbox.html", "work.html"}, names


def test_the_legacy_templates_are_exempt():
    """``layout.html`` and its children are not shell screens; this test must
    not silently claim anything about them."""
    legacy = [p for p in sorted(_TEMPLATES.glob("*.html"))
              if 'extends "layout.html"' in p.read_text(encoding="utf-8")]
    assert legacy, "no layout.html children found — the exemption is untested"
    assert not ({p.name for p in legacy} & {p.name for p in _shell_templates()})
