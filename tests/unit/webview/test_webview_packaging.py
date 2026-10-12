"""P4 — the webgate assets ship from the install.

Two guarantees:
1. ``pyproject.toml`` package-data ships the dev-tree ``webview/static/**`` and
   ``webview/templates/**`` so an installed wheel can serve the webgate (the
   ``web_dist`` bundle is still preferred when built).
2. ``core.assets.webgate_asset_dir()`` resolves to the repo ``webview/`` dir today
   (no packaged bundle present) — server.py mounts static/templates through it.
"""
from pathlib import Path

try:  # py3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

_REPO = Path(__file__).resolve().parents[3]


def _pyproject():
    with open(_REPO / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)


def test_packages_find_includes_webview():
    cfg = _pyproject()
    include = cfg["tool"]["setuptools"]["packages"]["find"]["include"]
    assert any(p.startswith("webview") for p in include), include


def test_package_data_ships_webview_static_and_templates():
    cfg = _pyproject()
    pkg_data = cfg["tool"]["setuptools"]["package-data"]
    assert "webview" in pkg_data, pkg_data.keys()
    globs = pkg_data["webview"]
    assert any("static/" in g for g in globs), globs
    assert any("templates/" in g for g in globs), globs


def test_no_package_data_row_for_a_package_that_does_not_exist():
    """D6 (2026-10-10): the old ``"polyrob" = ["web_dist/**/*"]`` row named a
    package the wheel does not ship (the bundle was never built); every
    package-data key must be a shipped top-level package or ``*``."""
    cfg = _pyproject()
    pkg_data = cfg["tool"]["setuptools"]["package-data"]
    find = cfg["tool"]["setuptools"]["packages"]["find"]
    roots = [_REPO / w for w in find["where"]]
    for key in pkg_data:
        if key == "*":
            continue
        assert any((r / key.replace(".", "/")).is_dir() for r in roots), key


def test_webgate_asset_dir_falls_back_to_repo_webview_today():
    import core.assets as assets
    d = assets.webgate_asset_dir()
    # No packaged web_dist in the dev tree → repo webview/ fallback.
    assert d.name == "webview"
    assert (d / "static").is_dir()
    assert (d / "templates").is_dir()


def test_webview_is_a_package():
    """webview must be importable as a package so setuptools find discovers it."""
    assert (_REPO / "webview" / "__init__.py").is_file()
