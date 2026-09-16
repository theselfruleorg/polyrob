"""043 A14(S) — tools/dom/buildDomTree.js must ship in the installed wheel,
and the in-process fallback (used when it doesn't) must actually return a
value instead of silently discarding it.

``tools/dom/service.py::DomService.__init__`` reads ``buildDomTree.js`` by
path at runtime. The ``[tool.setuptools.package-data]`` table in
``pyproject.toml`` had no entry for ``tools/`` JS assets, so a `pip install`
of the published wheel shipped no ``.js`` file there at all — every
installed-wheel run of the browser tool silently fell back to a minimal
DOM-tree builder. Worse, that fallback script called
``buildDomTree(arguments[0]);`` as a bare statement (not returned) at the
top level of a string handed to Playwright's ``page.evaluate`` — since
Playwright's Python client never sets ``isFunction`` explicitly, the
resulting eval runs as an *indirect* eval in the page's global scope, where
top-level ``arguments`` isn't bound at all: the fallback actually THREW
``ReferenceError: arguments is not defined`` rather than merely returning
``None`` (verified live against the bundled Playwright driver's
``UtilityScript.evaluate``). Wrapping the fallback in a real (non-arrow)
function expression and returning ``buildDomTree(arguments[0])`` from it
fixes both: the whole snippet evaluates to a callable Playwright
auto-invokes, and calling it gives that function its own ``arguments``.
"""
import fnmatch
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_dom_js_is_package_data():
    cfg = tomllib.load(open(REPO_ROOT / "pyproject.toml", "rb"))
    package_data = cfg["tool"]["setuptools"]["package-data"]
    globs = package_data.get("tools", [])
    assert any(fnmatch.fnmatch("dom/buildDomTree.js", g) for g in globs), (
        f"no tools package-data glob matches dom/buildDomTree.js: {globs!r}"
    )


def test_build_dom_tree_js_exists_on_disk():
    # The asset the package-data glob is supposed to pick up must actually
    # be there for the glob to be meaningful.
    assert (REPO_ROOT / "tools" / "dom" / "buildDomTree.js").is_file()


def test_dom_service_fallback_returns_its_value():
    src = (REPO_ROOT / "tools" / "dom" / "service.py").read_text(encoding="utf-8")
    # Isolate the fallback JS literal (the triple-quoted self.js_code
    # assignment inside the except: block), not the whole file.
    m = re.search(r'self\.js_code = """(.*?)"""', src, re.S)
    assert m, "fallback self.js_code triple-quoted literal not found"
    fallback_js = m.group(1)
    assert "return buildDomTree(" in fallback_js
    # It must not still be a bare, unreturned statement.
    assert "\n\t\t\tbuildDomTree(arguments[0]);\n" not in fallback_js
