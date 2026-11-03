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

# Neither a source pyproject nor installed metadata: an unknown build. Not a
# release literal (W11) — a release bump edits pyproject.toml ONLY.
_FALLBACK_VERSION = "0+unknown"

# Project names this module is willing to claim from an adjacent pyproject.toml, so
# a parent/monorepo pyproject can never mislabel the version.
_OWN_PROJECT_NAMES = {"polyrob", "polyrob-core"}


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

    Written by ``scripts/deploy_prod.sh`` before the units start (a rollback
    restores the old stamp); absent on a dev checkout or a pip install. Fail-open: an unreadable file reads as None.
    """
    try:
        text = (_code_root() / ".deployed_sha").read_text(encoding="utf-8").strip()
    except Exception:
        return None
    return text[:12] or None


def running_version_line() -> str:
    """``vX.Y.Z (deployed abc123def456)`` — the version of the code that RUNS.

    095 W1a: the version is the one this process imported (``__version__``);
    the tree on disk is read again, and ``; on disk: vY`` is added only when it
    differs (an upgrade landed and the process has not restarted yet).
    """
    sha = deployed_sha()
    line = f"v{__version__}" + (f" (deployed {sha})" if sha else "")
    disk = get_version()
    return line + (f"; on disk: v{disk}" if disk != __version__ else "")


def deployed_from() -> dict:
    """The deploy provenance (``.deployed_from``: ``sha=``, ``previous=``, ``at=``,
    ``subject=``…) as a dict; ``{}`` when absent (a dev checkout, a pip install,
    a deploy older than 095 P0.3). An unreadable file is NOT absent: it returns
    ``{"_unreadable": "<error class>"}`` so a reader can say so. ``previous=none``
    reads as no key."""
    try:
        text = (_code_root() / ".deployed_from").read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except Exception as e:
        return {"_unreadable": type(e).__name__}
    out = {}
    for raw in text.splitlines():
        key, sep, value = raw.partition("=")
        value = value.strip()
        if sep and key.strip() and value and value.lower() != "none":
            out[key.strip()] = value
    return out


def pending_deploy_line() -> str:
    """095 W7: one line when the code on disk is not the code this process runs
    (a deploy or an upgrade landed and the process has not restarted), else ``""``.

    The running sha is the stamp this process read at import (``deploy_prod.sh``
    stamps ``.deployed_sha`` BEFORE the start, 095 P0.3); the disk one is read now.
    """
    disk_sha, disk_ver = deployed_sha(), get_version()
    parts = []
    if disk_sha and disk_sha != _STARTUP_SHA:
        parts.append(f"commit {disk_sha} (this process runs {_STARTUP_SHA or 'an unstamped tree'})")
    if disk_ver != __version__:
        parts.append(f"version v{disk_ver} (this process runs v{__version__})")
    if not parts:
        return ""
    return ("pending deploy: the code on disk is " + " and ".join(parts)
            + "; it applies at the next restart")


RELEASE_NOTES_MAX_CHARS = 6000


def changelog_path() -> Optional[Path]:
    """The ``CHANGELOG.md`` shipped with the running code, or None.

    A checkout / the prod deploy carry it at the code root; a wheel carries it
    as ``core/CHANGELOG.md`` (080 WS-5 D9: a symlink to the root file in the
    tree, package data in the wheel — site-packages has no code root file).
    """
    for path in (_code_root() / "CHANGELOG.md", _code_root() / "core" / "CHANGELOG.md"):
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def release_notes(version: str = "", max_chars: int = RELEASE_NOTES_MAX_CHARS) -> str:
    """One release's section of the ``CHANGELOG.md`` shipped with the running code.

    ``version`` empty / ``latest`` → the running version's section (or the newest
    released one when the running version has none); on a deploy
    (``deployed_sha()`` set) the ``[Unreleased]`` section comes first, because a
    deploy of ``main`` runs what is past the last tag (080 D9). ``unreleased`` →
    the ``[Unreleased]`` section. ``since_previous`` → what changed since the
    previous deploy (:func:`_since_previous`). Never raises; an absent file says
    so honestly.
    """
    path = changelog_path()
    if path is None:
        return "Release notes are not shipped with this install (no CHANGELOG.md next to the code)."
    try:
        text = path.read_text(encoding="utf-8")
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
    if want.replace("-", "_") in ("since_previous", "since_last_deploy", "last_update"):
        return _cap(_since_previous(path, sections, order), max_chars)
    deployed_unreleased = ""
    if want in ("", "latest"):
        want = get_version().lower()
        if want not in sections:
            want = next((k for k in order if k != "unreleased"), "")
        if deployed_sha() and sections.get("unreleased"):
            deployed_unreleased = sections["unreleased"]
    body = sections.get(want)
    if deployed_unreleased:
        body = deployed_unreleased + ("\n\n" + body if body else "")
    if not body:
        known = ", ".join(k for k in order[:8])
        return f"No release notes for {version!r} in CHANGELOG.md. Known: {known}."
    return _cap(body, max_chars)


def _cap(body: str, max_chars: int) -> str:
    if len(body) <= max_chars:
        return body
    cut = len(body) - max_chars
    return body[:max_chars] + f"\n…[truncated at {max_chars} chars; {cut} more chars cut]"


def _git_changelog_added(previous: str, current: str) -> Optional[list]:
    """The CHANGELOG.md lines added between two commits, or None when this tree
    has no git history that holds both (the prod tree is a ``git archive``)."""
    import re
    import subprocess
    root = _code_root()
    if not all(re.fullmatch(r"[0-9a-fA-F]{7,40}|HEAD", ref or "") for ref in (previous, current)):
        return None
    if not (root / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "diff", "--unified=0", previous, current, "--", "CHANGELOG.md"],
            capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return [ln[1:] for ln in proc.stdout.splitlines()
            if ln.startswith("+") and not ln.startswith("+++")]


def _since_previous(path: Path, sections: dict, order: list) -> str:
    """095 W7 (SK-8): the CHANGELOG change between the previous deploy and this one.

    1. ``.deployed_from`` names ``previous=`` (095 P0.3); without it: say so.
    2. A tree with git history: the CHANGELOG lines added between the two commits.
    3. Else, when ``.deployed_from`` records ``previous_version=``: every section
       newer than that version (``[Unreleased]`` included).
    4. Else: say that it cannot tell, and why.
    """
    rec = deployed_from()
    previous, current = rec.get("previous"), rec.get("sha") or deployed_sha()
    if not previous:
        why = ("no .deployed_from next to the code" if not rec
               else f".deployed_from is unreadable ({rec['_unreadable']})"
               if rec.get("_unreadable")
               else ".deployed_from records no previous deploy")
        return (f"Cannot tell what changed since the previous deploy: {why}. "
                "Read release_notes='latest' or 'unreleased' instead.")
    head = f"Changes since the previous deploy ({previous[:12]} -> {(current or 'this tree')[:12]}):"
    added = _git_changelog_added(previous, current or "HEAD")
    if added is not None:
        lines = [ln for ln in added if ln.strip()]
        if not lines:
            return f"{head}\nNo CHANGELOG.md line changed between the two commits."
        return head + "\n" + "\n".join(lines)
    prev_ver = (rec.get("previous_version") or "").strip().lower().lstrip("v")
    if prev_ver and prev_ver in sections:
        newer = order[:order.index(prev_ver)]
        if prev_ver == __version__.lower():
            note = ("Same version as the previous deploy: only the [Unreleased] section can "
                    "hold the change, and it may also list what the previous deploy ran.")
        else:
            note = f"The sections newer than v{prev_ver}:"
        body = "\n\n".join(sections[k] for k in newer)
        return f"{head}\n{note}\n\n{body}" if body else f"{head}\nNo newer section in CHANGELOG.md."
    return (f"Cannot tell what changed since the previous deploy {previous[:12]}: this tree "
            "has no git history and .deployed_from does not record the previous version. "
            "Read release_notes='latest' (the running version, and on a deploy the "
            "[Unreleased] section) — it may also list older changes.")


__version__ = get_version()
#: The deploy stamp this process read at import = the commit it RUNS (095 P0.3
#: stamps before the start). :func:`pending_deploy_line` compares the disk to it.
_STARTUP_SHA = deployed_sha()
