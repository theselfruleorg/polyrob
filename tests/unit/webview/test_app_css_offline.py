"""The console's stylesheet family — offline-complete, ONE design, and it ships.

The console stylesheet must remain fully offline:
``webview/static/app/app.css`` used to be ``docs/design/040/mockup.css`` byte for
byte, frozen by a test in this file, while every ``layout.html`` page kept the
refined terminal design in ``webview/static/css/``. Two visual products. The rule
now: **mockups are schemas, not a stylesheet.** ``app.css`` is the five
destinations' component sheet WRITTEN OVER the design of record's tokens
(``variables.css``), loaded after it, introducing no palette of its own.

Properties asserted here, each of which has failed in this tree before:

1. **No network.** Neither shell reaches Google Fonts or any CDN for its faces;
   JetBrains Mono + VT323 are vendored and declared in ``fonts.css``.
2. **Every ``url()`` resolves** to a file inside the tree.
3. **One family.** ``shell.html`` loads ``fonts → variables → components → style →
   app`` — the same order ``layout.html`` uses — and ``app.css`` binds every schema
   token name to a ``variables.css`` token rather than to a colour of its own.
4. **Every class the shell and its modules use is defined** somewhere in the
   family, so a rename cannot silently unstyle a component.
5. **It is not the mockup.** ``app.css`` diverges from ``mockup.css`` on purpose.
6. **The fonts ship** (package-data globs + MANIFEST graft).
"""
import glob
import os
import re
from pathlib import Path

import pytest

try:  # py3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

_REPO = Path(__file__).resolve().parents[3]
_WEBVIEW = _REPO / "webview"
_APP_CSS = _WEBVIEW / "static" / "app" / "app.css"
_FONTS_CSS = _WEBVIEW / "static" / "css" / "fonts.css"
_VARIABLES_CSS = _WEBVIEW / "static" / "css" / "variables.css"
_FONTS = _WEBVIEW / "static" / "fonts"
_SHELL = _WEBVIEW / "templates" / "shell.html"
_LAYOUT = _WEBVIEW / "templates" / "layout.html"
_MOCKUP_CSS = _REPO / "docs" / "design" / "040" / "mockup.css"

_HAVE_MOCKUPS = _MOCKUP_CSS.is_file()
_needs_mockups = pytest.mark.skipif(
    not _HAVE_MOCKUPS, reason="mockup set is absent from the public tree")

#: The design of record has ONE text face and ONE display face.
_EXPECTED_FONT_FILES = {
    "JetBrainsMono-Regular.woff2",
    "JetBrainsMono-Medium.woff2",
    "VT323-Regular.woff2",
}

#: The order the design system has always been loaded in (layout.html), plus the
#: destinations' sheet last.
_FAMILY = ["/static/css/fonts.css", "/static/css/variables.css",
           "/static/css/components.css", "/static/css/style.css",
           "/static/app/app.css"]

#: The mockup's palette. app.css may alias the schema's token NAMES, never
#: re-declare its COLOURS — that is what made two products.
_MOCKUP_HEX = ("#0B121C", "#111A26", "#18222F", "#1F2B3A", "#24303F", "#33414F",
               "#5D7790", "#E6EDF5", "#97A6B8", "#8493A6", "#3FB980", "#E0A33E",
               "#E46D67", "#4EA8DE", "#3676A6")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _family_css() -> str:
    return "\n".join(_read(p) for p in list((_WEBVIEW / "static" / "css").rglob("*.css"))
                     + [_APP_CSS])


# --- 1. no network ----------------------------------------------------------- #

@pytest.mark.parametrize("path", [_APP_CSS, _FONTS_CSS])
def test_no_network_reference_anywhere(path):
    css = _read(path)
    assert "http" not in css, f"{path.name} reaches the network"
    assert "//fonts." not in css and "url(//" not in css, "protocol-relative url"
    assert "@import" not in css, "an @import is a second fetch"


@pytest.mark.parametrize("template", [_SHELL, _LAYOUT])
def test_neither_shell_loads_fonts_from_a_cdn(template):
    html = _read(template)
    assert "fonts.googleapis" not in html and "fonts.gstatic" not in html
    assert "/static/css/fonts.css" in html


# --- 2. every url resolves --------------------------------------------------- #

@pytest.mark.parametrize("path", [_APP_CSS, _FONTS_CSS])
def test_every_url_resolves_to_a_file_in_the_tree(path):
    css = _read(path)
    refs = re.findall(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", css)
    for ref in refs:
        assert not ref.startswith(("http", "//", "data:")), ref
        target = (path.parent / ref).resolve()
        assert target.is_file(), f"url({ref}) resolves to nothing: {target}"
        assert _REPO in target.parents, f"url({ref}) escapes the tree"


def test_the_faces_are_vendored_and_declared():
    css = _read(_FONTS_CSS)
    on_disk = {p.name for p in _FONTS.glob("*.woff2")}
    assert on_disk == _EXPECTED_FONT_FILES, f"vendored fonts drifted: {on_disk}"
    for name in _EXPECTED_FONT_FILES:
        assert name in css, f"{name} is vendored but no @font-face uses it"
    assert css.count("@font-face") == len(_EXPECTED_FONT_FILES)
    assert "@font-face" not in _read(_APP_CSS), "faces are declared once, in fonts.css"


def test_each_family_keeps_its_licence():
    """OFL 1.1 requires the licence to travel with the font."""
    for family in ("JetBrainsMono", "VT323"):
        lic = _FONTS / f"LICENSE-{family}"
        assert lic.is_file(), f"{lic.name} is missing"
        assert "SIL Open Font License" in _read(lic)


# --- 3. one family ----------------------------------------------------------- #

def test_the_shell_loads_the_design_of_record_in_order():
    html = _read(_SHELL)
    positions = [html.find(href) for href in _FAMILY]
    assert all(p >= 0 for p in positions), f"shell.html is missing a sheet: {_FAMILY}"
    assert positions == sorted(positions), "the family must load in the layout.html order"
    assert html.count('rel="stylesheet"') == len(_FAMILY)


def test_app_css_declares_no_palette_of_its_own():
    css = _read(_APP_CSS).upper()
    for hexval in _MOCKUP_HEX:
        assert hexval.upper() not in css, f"app.css re-declares the mockup colour {hexval}"
    assert "IBM PLEX" not in css, "the design of record has no sans face"


def test_app_css_binds_the_schema_tokens_to_variables_css():
    """Every schema token the templates say inline (``var(--s4)``, ``var(--surface-1)``…)
    must resolve: either app.css declares it, or variables.css does."""
    declared = set(re.findall(r"(--[a-z][a-z0-9-]*)\s*:", _read(_APP_CSS) + _read(_VARIABLES_CSS)))
    used = set()
    for path in list((_WEBVIEW / "templates").glob("*.html")) + list((_WEBVIEW / "static" / "app").glob("*.js")):
        used |= set(re.findall(r"var\((--[a-z][a-z0-9-]*)\)", _read(path)))
    missing = sorted(used - declared)
    assert not missing, f"tokens said inline but declared nowhere: {missing}"
    # The colour tokens are ALIASES of the design of record, not new values.
    for name in ("--ground", "--surface-1", "--ink", "--running", "--stopped", "--accent"):
        m = re.search(re.escape(name) + r"\s*:\s*([^;]+);", _read(_APP_CSS))
        assert m, f"{name} is not bound in app.css"
        assert "var(--color-" in m.group(1), f"{name} must alias a variables.css colour, got {m.group(1)!r}"


# --- 4. every class used is defined ------------------------------------------ #

_NOT_CLASSES = {"if", "else", "endif", "endfor", "for", "in", "not", "and", "or"}
_CLASS_ATTR = re.compile(r'class(?:Name)?\s*=\s*["\']([^"\'{}]+)["\']')
_EL_CALL = re.compile(r'''el\(\s*["'][a-z0-9]+["']\s*,\s*["']([^"'{}$]+)["']''')
_CLASSLIST = re.compile(r'''classList\.(?:add|toggle)\(\s*["']([^"']+)["']''')


def _used_classes() -> set:
    used = set()
    sources = list((_WEBVIEW / "templates").glob("*.html")) + list((_WEBVIEW / "static" / "app").glob("*.js"))
    for path in sources:
        text = _read(path)
        for rx in (_CLASS_ATTR, _EL_CALL, _CLASSLIST):
            for hit in rx.findall(text):
                for token in hit.split():
                    if token in _NOT_CLASSES or "." in token or "(" in token or "%" in token:
                        continue
                    used.add(token)
    return used


def test_every_class_the_console_uses_is_defined_in_the_family():
    css = _family_css()
    missing = sorted(c for c in _used_classes()
                     if not re.search(r"\." + re.escape(c) + r"(?![\w-])", css))
    assert not missing, f"classes used by the console but styled nowhere: {missing}"


def test_the_scan_is_not_vacuous():
    used = _used_classes()
    assert {"nav-item", "entry", "pill", "unknown", "sources", "composer"} <= used


# --- 5. it is not the mockup ------------------------------------------------- #

@_needs_mockups
def test_app_css_is_not_the_mockup_stylesheet():
    """The mockups are the SCHEMA. Freezing their stylesheet as the product's was
    the 043 regression; this pins the reversal."""
    mockup = _read(_MOCKUP_CSS)
    css = _read(_APP_CSS)
    assert css != mockup
    tail = mockup.split("@import", 1)[-1]
    assert not css.endswith(tail[-400:]), "app.css has drifted back onto the mockup"


# --- 6. it ships ------------------------------------------------------------- #

def _pyproject() -> dict:
    with open(_REPO / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)


def test_package_data_globs_match_the_asset_dirs():
    """Asserted by RESOLVING the glob, not by matching its text."""
    globs = _pyproject()["tool"]["setuptools"]["package-data"]["webview"]
    cwd = os.getcwd()
    os.chdir(_WEBVIEW)
    try:
        shipped = {p for g in globs for p in glob.glob(g, recursive=True)}
    finally:
        os.chdir(cwd)
    assert "static/app/app.css" in shipped
    assert "static/css/fonts.css" in shipped
    for name in _EXPECTED_FONT_FILES:
        assert f"static/fonts/{name}" in shipped, f"no package-data glob ships static/fonts/{name}"


def test_manifest_grafts_the_assets_into_the_sdist():
    manifest = _read(_REPO / "MANIFEST.in")
    assert "graft webview/static" in manifest
