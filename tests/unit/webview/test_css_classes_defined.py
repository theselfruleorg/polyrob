"""P3 — the 8 formerly-undefined CSS classes are now defined for real.

STYLES_REFACTOR_PLAN flagged ~8 classes referenced by templates but defined in no
stylesheet (`.btn-primary`, `.btn-search`, `.btn-page`, `.admin-nav`/`.admin-nav-item`,
`.section-header`, `.search-input`, `.filter-select`, `.loading`). The targeted
cleanup (Q4=B) makes each one resolve to a real definition in some
``webview/static/css/**/*.css`` file (the design system's components.css, plus the
de-inlined pages/*.css). This is the objective bar — no "TEMPORARY until migration"
hacks.
"""
import re
from pathlib import Path

import pytest

_WEBVIEW_ROOT = Path(__file__).resolve().parents[3] / "webview"
_CSS_ROOT = _WEBVIEW_ROOT / "static" / "css"
_TEMPLATES_ROOT = _WEBVIEW_ROOT / "templates"

# (selector, ...) — every one must be defined in at least one css file.
_SELECTORS = [
    "btn-primary",
    "btn-search",
    "btn-page",
    "admin-nav-item",
    "admin-nav",
    "section-header",
    "search-input",
    "filter-select",
    "loading",
]

# 030 D-6 — the webgate component classes are not a hardcoded list: scan the
# templates for every `webgate-*` class token actually referenced (both HTML
# `class="…"` attributes and JS `el.className = '…'` assignments). (043 §9 phase 4
# deleted the memory/knowledge/config pages that were the only users of
# `webgate-caption`/`webgate-empty`/`webgate-item`; the surviving legacy pages are
# layout.html + pending.html.)
_CLASS_ASSIGN_RE = re.compile(r"""class(?:Name)?\s*=\s*["']([^"']+)["']""")
_WEBGATE_TOKEN_RE = re.compile(r"webgate-[A-Za-z0-9_-]+")


def _webgate_classes_in_templates() -> list:
    found = set()
    for tpl in sorted(_TEMPLATES_ROOT.rglob("*.html")):
        text = tpl.read_text(encoding="utf-8", errors="ignore")
        for match in _CLASS_ASSIGN_RE.finditer(text):
            for token in match.group(1).split():
                if _WEBGATE_TOKEN_RE.fullmatch(token):
                    found.add(token)
    return sorted(found)


_WEBGATE_CLASSES = _webgate_classes_in_templates()


def _all_css_text() -> str:
    parts = []
    for css in _CSS_ROOT.rglob("*.css"):
        parts.append(css.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(parts)


def test_css_root_exists():
    assert _CSS_ROOT.is_dir(), f"missing css root {_CSS_ROOT}"


@pytest.mark.parametrize("selector", _SELECTORS)
def test_selector_defined_somewhere(selector):
    text = _all_css_text()
    # A real definition is `.<selector>` used as a selector (followed by a
    # combinator / brace / comma / pseudo / whitespace), not a substring of a
    # longer class name.
    pattern = re.compile(r"\." + re.escape(selector) + r"(?![\w-])")
    assert pattern.search(text), f".{selector} is not defined in any {_CSS_ROOT} css file"


def test_webgate_scan_finds_the_known_core_classes():
    """Guard the scanner itself — if the regex rots, the parametrized list
    silently shrinks to nothing and the suite goes green vacuously."""
    for expected in ("webgate-page", "webgate-header", "webgate-list",
                     "webgate-pause-headline", "webgate-readonly-banner"):
        assert expected in _WEBGATE_CLASSES, (
            f"template scan lost {expected!r} — scanner regex or templates changed"
        )


@pytest.mark.parametrize("selector", _WEBGATE_CLASSES)
def test_webgate_class_defined_in_css(selector):
    """030 D-6: every webgate-* class a template references must resolve to a
    real definition in some webview/static/css/**/*.css file (webgate.css is
    the component layer) — inline template <style> blocks don't count."""
    text = _all_css_text()
    pattern = re.compile(r"\." + re.escape(selector) + r"(?![\w-])")
    assert pattern.search(text), f".{selector} is not defined in any {_CSS_ROOT} css file"


def test_no_temporary_hack_blocks_remain():
    """The Part-4 'TEMPORARY until migration' shims must be gone — real components cover them."""
    style = (_CSS_ROOT / "style.css").read_text(encoding="utf-8", errors="ignore")
    assert "TEMPORARY until migration" not in style
