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

_CSS_ROOT = Path(__file__).resolve().parents[3] / "webview" / "static" / "css"

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


def test_no_temporary_hack_blocks_remain():
    """The Part-4 'TEMPORARY until migration' shims must be gone — real components cover them."""
    style = (_CSS_ROOT / "style.css").read_text(encoding="utf-8", errors="ignore")
    assert "TEMPORARY until migration" not in style
