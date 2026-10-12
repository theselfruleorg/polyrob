"""THE dependency spec at runtime: ``pyproject.toml`` extras (W11).

``[project.optional-dependencies]`` in ``pyproject.toml`` is the ONE place a
feature's distributions and their version clauses are written. Runtime tables
that need them (``core.lazy_deps.LAZY_DEPS``, the updater's extras shape in
``cli.update.extras``) read them here instead of carrying a copy.

Resolution, like ``core.version``: the source checkout's ``pyproject.toml``
first (an editable install's metadata only refreshes on reinstall, so it can
miss an extra added since), else the installed ``polyrob`` distribution's
``Requires-Dist: X; extra == "name"`` markers (a wheel install has no
pyproject). Neither → ``{}``; callers fall back to bare names.

Stdlib-only (plus ``importlib.metadata``); no imports above core.
"""
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Tuple

DIST = "polyrob"
#: Tooling or aliases, never a capability.
META_EXTRAS = frozenset({"dev", "all"})
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")
_EXTRA_MARKER_RE = re.compile(r"""extra\s*==\s*['"]([A-Za-z0-9._-]+)['"]""")
_SOURCE_ROOT = Path(__file__).resolve().parents[1]


def dist_name(spec: str) -> str:
    """``anthropic>=0.20.0`` -> ``anthropic``; ``uvicorn[standard]==1`` -> ``uvicorn``
    (lower-cased, ``_`` and ``.`` folded to ``-``: the PEP 503 name)."""
    m = _NAME_RE.match((spec or "").strip())
    return re.sub(r"[-_.]+", "-", (m.group(0) if m else spec or "")).lower()


def _from_pyproject(path: Path) -> Optional[Dict[str, Tuple[str, ...]]]:
    try:
        import tomllib
        project = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
    except Exception:  # noqa: BLE001 — unreadable: try the metadata
        return None
    if str(project.get("name", "")).strip().lower() != DIST:
        return None
    return {str(extra): tuple(str(s).strip() for s in specs)
            for extra, specs in (project.get("optional-dependencies") or {}).items()}


def _from_metadata() -> Dict[str, Tuple[str, ...]]:
    try:
        from importlib.metadata import distribution
        requires = distribution(DIST).requires or ()
    except Exception:  # noqa: BLE001 — no metadata
        return {}
    out: Dict[str, list] = {}
    for req in requires:
        m = _EXTRA_MARKER_RE.search(req)
        if m:
            out.setdefault(m.group(1), []).append(req.split(";", 1)[0].strip())
    return {k: tuple(v) for k, v in out.items()}


def extra_specs(root: Optional[Path] = None, *, include_meta: bool = False,
                ) -> Dict[str, Tuple[str, ...]]:
    """``extra -> its requirement specs`` (markers stripped; the self-referencing
    ``polyrob[...]`` rows dropped). *root* = a repo root whose ``pyproject.toml``
    to read; default = this source checkout, else the installed metadata."""
    return dict(_cached(str(root) if root else "", include_meta))


@lru_cache(maxsize=8)
def _cached(root: str, include_meta: bool) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    path = Path(root or _SOURCE_ROOT) / "pyproject.toml"
    data = _from_pyproject(path) if path.is_file() else None
    if data is None:
        data = _from_metadata()
    out = []
    for extra, specs in sorted(data.items()):
        if extra in META_EXTRAS and not include_meta:
            continue
        out.append((extra, tuple(s for s in specs if dist_name(s) != DIST)))
    return tuple(out)


def extra_names(root: Optional[Path] = None) -> Dict[str, frozenset]:
    """``extra -> the distribution names it declares`` (meta extras excluded)."""
    return {extra: frozenset(dist_name(s) for s in specs)
            for extra, specs in extra_specs(root).items()}


__all__ = ["DIST", "META_EXTRAS", "dist_name", "extra_names", "extra_specs"]
