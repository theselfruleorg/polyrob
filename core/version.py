"""Single source of truth for the project version at runtime.

Build-time SSOT is pyproject.toml. Resolution order at runtime:

1. The **source** ``pyproject.toml`` next to this checkout — authoritative when you
   run ``polyrob`` from a source tree. This is FIRST on purpose: a stale editable
   or wheel install (e.g. a venv still carrying ``polyrob 1.0.0`` while the source
   is ``0.4.2``) must NOT shadow the version of the code you're actually running.
2. Installed package metadata — for a real ``pip install`` where there's no source
   ``pyproject.toml`` adjacent to this module (site-packages).
3. The literal fallback below (tests pin it to pyproject.toml so it can't drift).
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Optional

# Dev-checkout fallback. MUST equal pyproject.toml [project].version.
_FALLBACK_VERSION = "0.8.0"

# Project names this module is willing to claim from an adjacent pyproject.toml, so
# a parent/monorepo pyproject can never mislabel the version.
_OWN_PROJECT_NAMES = {"polyrob", "polyrob-core", "rob"}


def _load_toml(text: str) -> Optional[dict]:
    try:
        import tomllib  # py3.11+
    except ModuleNotFoundError:  # pragma: no cover - older interpreters
        try:
            import tomli as tomllib  # type: ignore
        except ModuleNotFoundError:
            return None
    try:
        return tomllib.loads(text)
    except Exception:
        return None


def _source_pyproject_version() -> Optional[str]:
    """Version from the checkout's own ``pyproject.toml``, or None.

    ``core/version.py`` → repo root is ``parents[1]``. Only trusted when the file's
    ``[project].name`` is one of ours, so a wrong/parent pyproject is ignored. Fully
    fail-open: any read/parse problem returns None and we fall through to metadata.
    """
    try:
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        if not pyproject.is_file():
            return None
        data = _load_toml(pyproject.read_text(encoding="utf-8"))
        if not data:
            return None
        project = data.get("project", {}) or {}
        name = str(project.get("name", "")).strip().lower()
        ver = project.get("version")
        if ver and name in _OWN_PROJECT_NAMES:
            return str(ver)
    except Exception:
        return None
    return None


def get_version() -> str:
    src = _source_pyproject_version()
    if src:
        return src
    try:
        return _pkg_version("polyrob")
    except PackageNotFoundError:
        return _FALLBACK_VERSION


__version__ = get_version()
