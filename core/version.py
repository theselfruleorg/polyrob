"""Single source of truth for the project version at runtime.

Build-time SSOT is pyproject.toml. Resolution order at runtime:

1. The **source** ``pyproject.toml`` next to this checkout — authoritative when you
   run ``polyrob`` from a source tree. This is FIRST on purpose: a stale editable
   or wheel install (e.g. a venv still carrying ``polyrob 0.13.0`` while the source
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
_FALLBACK_VERSION = "1.3.0"

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


def _code_root() -> Path:
    """The directory the running code lives in (``/opt/polyrob`` on the box)."""
    return Path(__file__).resolve().parents[1]


def deployed_sha() -> Optional[str]:
    """The commit a deploy stamped next to the code (``.deployed_sha``), or None.

    Written by ``scripts/deploy_prod.sh`` after a verified restart; absent on a
    dev checkout or a pip install. Fail-open: an unreadable file reads as None.
    """
    try:
        text = (_code_root() / ".deployed_sha").read_text(encoding="utf-8").strip()
    except Exception:
        return None
    return text[:12] or None


def running_version_line() -> str:
    """``vX.Y.Z (deployed abc123def456)`` — the version of the code that RUNS."""
    sha = deployed_sha()
    return f"v{get_version()}" + (f" (deployed {sha})" if sha else "")


RELEASE_NOTES_MAX_CHARS = 6000


def release_notes(version: str = "", max_chars: int = RELEASE_NOTES_MAX_CHARS) -> str:
    """One release's section of the ``CHANGELOG.md`` shipped with the running code.

    ``version`` empty / ``latest`` → the running version's section (or the newest
    released one when the running version has none). ``unreleased`` → the
    ``[Unreleased]`` section. Never raises; an absent file says so honestly.
    """
    path = _code_root() / "CHANGELOG.md"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "Release notes are not shipped with this install (no CHANGELOG.md next to the code)."
    except Exception as exc:
        return f"Release notes unreadable ({type(exc).__name__}: {path})."
    import re
    sections = {}
    order = []
    for m in re.finditer(r"^## \[([^\]]+)\][^\n]*\n(.*?)(?=^## \[|\Z)", text, re.M | re.S):
        key = m.group(1).strip().lower()
        # Drop trailing markdown link definitions ("[1.0.0]: https://…").
        sections[key] = re.sub(r"(?m)^\[[^\]]+\]:\s.*\n?", "", m.group(0)).strip()
        order.append(key)
    want = (version or "").strip().lower().lstrip("v")
    if want in ("", "latest"):
        want = get_version().lower()
        if want not in sections:
            want = next((k for k in order if k != "unreleased"), "")
    body = sections.get(want)
    if not body:
        known = ", ".join(k for k in order[:8])
        return f"No release notes for {version!r} in CHANGELOG.md. Known: {known}."
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n…[truncated at {max_chars} chars]"
    return body


__version__ = get_version()
