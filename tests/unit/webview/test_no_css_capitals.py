"""070 E.2 — the console never transforms case.

Every console word renders as the copy layer wrote it (sentence case). The
console sheet carries no ``text-transform: uppercase``; the two shared rules that
capitalise for the legacy pages (``components.css`` ``.btn`` and ``.table th``)
are reset inside ``.console-shell``; and the shell loads a fixed set of sheets,
so a new one cannot bring capitals in unseen.
"""
import re
from pathlib import Path

_WEBVIEW = Path(__file__).resolve().parents[3] / "webview"
_APP_CSS = _WEBVIEW / "static" / "app"
_SHELL = _WEBVIEW / "templates" / "shell.html"

_UPPER = re.compile(r"text-transform\s*:\s*uppercase", re.I)


def test_app_css_has_no_uppercase():
    found = [f"{p.name}:{n}" for p in sorted(_APP_CSS.glob("*.css"))
             for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
             if _UPPER.search(line)]
    assert not found, f"the console capitalises text: {found}"


def test_shell_resets_the_shared_uppercase_rules():
    css = (_APP_CSS / "app.css").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        names = {s.strip() for s in selectors.split(",")}
        if ({".console-shell .btn", ".console-shell .table th"} <= names
                and re.search(r"text-transform\s*:\s*none", body)):
            return
    raise AssertionError("app.css must reset .console-shell .btn and "
                         ".console-shell .table th to text-transform:none")


def test_the_shell_loads_only_the_known_stylesheets():
    html = _SHELL.read_text(encoding="utf-8")
    sheets = [re.sub(r"\?.*$", "", h) for h in
              re.findall(r'<link rel="stylesheet" href="([^"]+)"', html)]
    assert sheets == [
        "/static/css/fonts.css",
        "/static/css/variables.css",
        "/static/css/components.css",
        "/static/css/style.css",
        "/static/app/app.css",
    ], sheets
