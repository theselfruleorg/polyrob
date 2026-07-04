"""Guards for the public landing (doc 04). The landing is the rob INSTANCE showcase,
not the polyrob framework marketing site. Load-bearing: the test-bot link is gone.
"""

from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
LANDING = REPO / "deployment" / "placeholder" / "index.html"


def _html() -> str:
    return LANDING.read_text()


class _Tolerant(HTMLParser):
    """A parse that simply must not raise on the landing markup."""
    def error(self, message):  # pragma: no cover - py<3.10 compat
        raise AssertionError(message)


def test_landing_parses_as_html():
    text = _html()
    assert text.lower().lstrip().startswith("<!doctype html>")
    assert "<html" in text and "</html>" in text
    _Tolerant().feed(text)  # must not raise


def test_no_test_bot_link():
    """The throwaway @testestovichbot link MUST NOT appear (the load-bearing guard)."""
    assert "testestovichbot" not in _html()
    assert "testesto" not in _html()


def test_run_your_own_cta_and_docs_present():
    text = _html()
    # OSS repo CTA — the locked URL OR the substitution token (doc 05 pins the URL).
    assert ("__OSS_REPO_URL__" in text) or ("github.com" in text), "missing OSS repo CTA"
    assert ("__DOCS_URL__" in text) or ("docs" in text.lower()), "missing Docs link"
    assert "run your own" in text.lower(), "primary CTA 'Run your own' missing"


def test_instance_and_framework_named():
    text = _html().lower()
    assert "rob" in text  # the instance
    assert "polyrob" in text  # the framework attribution


def test_no_external_js_framework():
    """Keep it static — no <script src=...> external JS framework (inline year-stamp OK)."""
    text = _html().lower()
    assert "<script src=" not in text


def test_no_test_bot_anywhere_in_deployment():
    """Aggregate: zero testestovichbot across the whole deployment/ tree (landing + nginx)."""
    hits = []
    for p in (REPO / "deployment").rglob("*"):
        if p.is_file() and p.suffix in (".html", ".conf", ".css", ".sh", ".example"):
            try:
                if "testestovichbot" in p.read_text(errors="ignore"):
                    hits.append(str(p.relative_to(REPO)))
            except OSError:
                pass
    assert not hits, f"test-bot link still present in: {hits}"
