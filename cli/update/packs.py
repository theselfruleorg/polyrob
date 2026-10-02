"""The first-party packs across an update (067).

067 (one install, owner decision 2026-09-25): the first-party packs ship INSIDE
the ``polyrob`` distribution — a tree that says so declares them as
``polyrob.packs`` entry points in its own ``pyproject.toml``
(:func:`tree_bundles_packs`). Updating to such a tree:

1. retires every RETIRED separate pack distribution this install still has
   (``core.packs.index.RETIRED_DISTS``) AFTER the project install, through the
   target tree's ``core/packs/retire.py`` (metadata only — never a pack file,
   which polyrob owns now; then one provider per pack, verified), before the
   update's verify (:func:`retire_commands`);
2. carries each such pack as its SDK extra (x -> twitter, …; the tree's
   ``pack.toml`` names it, :func:`bundled_extras`), so the SDKs keep arriving.

A tree WITHOUT that declaration (a rollback target from the separate-dist layout)
still installs its packs its own way: the tree's resolver (``core/lock_closure.py
packs --list``) names the pack directories, each installs ``--no-deps`` after the
hashed closure (:func:`resolve_install`, :func:`pack_commands`).

Every pack a rollback would install — the resolved set, dependencies included —
is checked against the pack kill list (:func:`kill_reason`) by id AND dist at the
tree's version; a killed pack, and a pack that depends on one, is skipped by name.
Third-party packs are never touched.
"""
from __future__ import annotations

import importlib.metadata as _md
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

PACK_ENTRY_GROUP = "polyrob.packs"


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name)).lower()


@dataclass(frozen=True)
class InstalledPack:
    id: str
    dist: str        # canonical
    version: str


@dataclass(frozen=True)
class TreePack:
    id: str
    dist: str        # canonical
    version: str
    path: Path


def installed_packs() -> List[InstalledPack]:
    """Every (id, distribution) with a ``polyrob.packs`` entry point, sorted by
    id then dist — a leftover retired dist beside polyrob shows as its own row."""
    out: Dict[Tuple[str, str], InstalledPack] = {}
    try:
        eps = _md.entry_points(group=PACK_ENTRY_GROUP)
    except Exception:  # noqa: BLE001 — unreadable metadata = nothing to carry
        return []
    for ep in eps:
        dist = getattr(ep, "dist", None)
        if dist is None:
            continue
        name = _canon(dist.metadata["Name"] or "")
        out[(ep.name, name)] = InstalledPack(id=ep.name, dist=name, version=str(dist.version))
    return [out[k] for k in sorted(out)]


def _retired_names() -> frozenset:
    """``core.packs.index.RETIRED_DISTS`` — THE list. A core that predates it
    has nothing to retire."""
    try:
        from core.packs.index import RETIRED_DISTS
    except ImportError:
        return frozenset()
    return frozenset(RETIRED_DISTS)


def retired_installed(installed: Optional[List[InstalledPack]] = None) -> List[InstalledPack]:
    """The installed packs whose distribution is a retired separate pack name."""
    names = _retired_names()
    have = installed if installed is not None else installed_packs()
    return [p for p in have if p.dist in names]


def _pyproject(repo_root: Optional[Path]) -> dict:
    if not repo_root:
        return {}
    import tomllib
    try:
        return tomllib.loads((Path(repo_root) / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def tree_bundles_packs(repo_root: Optional[Path]) -> bool:
    """The tree's own pyproject declares the first-party packs (067, one install)."""
    project = _pyproject(repo_root).get("project", {})
    return bool((project.get("entry-points") or {}).get(PACK_ENTRY_GROUP))


def bundled_extras(repo_root: Optional[Path], ids: List[str]) -> List[str]:
    """The SDK extras the tree's ``pack.toml`` files name for pack *ids* (sorted).
    An id the tree does not bundle, or a manifest that cannot be read, adds none."""
    import tomllib
    eps = (_pyproject(repo_root).get("project", {}).get("entry-points") or {}).get(
        PACK_ENTRY_GROUP) or {}
    out = set()
    for pack_id in ids:
        ref = eps.get(pack_id)
        if not ref:
            continue
        package = str(ref).partition(":")[0].split(".", 1)[0]
        for manifest in sorted((Path(repo_root) / "packs").glob(f"*/{package}/pack.toml")):
            try:
                extra = tomllib.loads(manifest.read_text(encoding="utf-8")).get("extra")
            except (OSError, ValueError):
                continue
            if extra:
                out.add(str(extra))
    return sorted(out)


def retire_commands(python: str, repo: Path, retired: List[InstalledPack]) -> List[List[str]]:
    """The TARGET tree's retire helper (``core/packs/retire.py``: metadata only,
    never a pack file; then one provider per pack, verified), run AFTER the
    project install. Empty when nothing is retired."""
    if not retired:
        return []
    return [[python, str(Path(repo) / "core" / "packs" / "retire.py")]]


def retired_note(installed: Optional[List[InstalledPack]] = None) -> List[str]:
    """Owner-facing lines for a leftover retired pack distribution (empty when none)."""
    left = retired_installed(installed)
    if not left:
        return []
    names = " ".join(sorted({p.dist for p in left}))
    return [f"This install still has the retired pack distribution(s) {names}: the "
            f"first-party packs ship inside polyrob now. The next `polyrob update --apply` "
            f"retires them; to do it now: python core/packs/retire.py (from a polyrob "
            f"source tree) — never pip uninstall: it deletes pack files polyrob owns"]


# --- a rollback target with the separate-dist layout ------------------------ #

def tree_packs(repo_root: Optional[Path]) -> List[TreePack]:
    """The separately distributed packs of an OLD tree (``packs/*/pyproject.toml``).
    Empty for a tree that bundles its packs."""
    if not repo_root:
        return []
    import tomllib
    out: List[TreePack] = []
    for pp in sorted((Path(repo_root) / "packs").glob("*/pyproject.toml")):
        try:
            project = tomllib.loads(pp.read_text(encoding="utf-8"))["project"]
            eps = (project.get("entry-points") or {}).get(PACK_ENTRY_GROUP) or {}
            if len(eps) != 1:
                continue
            (pack_id,) = eps
            out.append(TreePack(id=str(pack_id), dist=_canon(project["name"]),
                                version=str(project.get("version", "")), path=pp.parent))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def carried_packs(repo_root: Optional[Path],
                  installed: Optional[List[InstalledPack]] = None) -> List[str]:
    """Ids of the installed packs this update carries: the retired separate
    dists (their pack now ships inside polyrob — or, on a rollback to an old
    tree, installs again from it) and an old tree's own first-party packs."""
    have = installed if installed is not None else installed_packs()
    tree = {p.id: p for p in tree_packs(repo_root)}
    ids = {p.id for p in retired_installed(have)}
    ids |= {p.id for p in have if p.id in tree and tree[p.id].dist == p.dist}
    return sorted(ids)


def kill_reason(dist: str, version: Optional[str]) -> Optional[str]:
    """Why *dist* (a pack id or distribution) at *version* must not be installed, or None.

    The one kill list is ``core.packs.index.killed`` (067 P7). This updater may
    run from a core that predates it; then there is no list to consult and the
    answer is None (the loader still refuses a killed pack at start)."""
    try:
        from core.packs.index import killed
    except ImportError:
        return None
    return killed(dist, version)


CaptureFn = Callable[[List[str], Path], str]


def resolve_install(python: str, repo: Path, wanted: List[str], capture: CaptureFn,
                    notes: List[str]) -> List[Tuple[str, str, str]]:
    """``[(id, dist, dir)]`` of the *wanted* packs an OLD tree installs as
    separate dists, dependencies first, killed packs removed. A tree that
    bundles its packs installs none this way (``[]``)."""
    if not wanted or tree_bundles_packs(repo):
        return []
    script = str(Path(repo) / "core" / "lock_closure.py")
    try:
        rows = _rows(capture([python, script, "packs", "--root", str(repo), "--ids", "all",
                              "--list"], repo))
    except Exception as exc:  # noqa: BLE001 — an old tree: nothing to resolve
        notes.append(f"packs not reinstalled: this tree cannot list its packs ({exc})")
        return []
    available = {r[0]: r for r in rows}
    versions = {p.id: p.version for p in tree_packs(repo)}

    def _killed(row) -> Optional[str]:
        # A kill row needs only an ``id`` (``dist`` is optional): ask by BOTH.
        v = versions.get(row[0])
        return kill_reason(row[0], v) or kill_reason(row[1], v)

    def _resolve(ids: List[str]) -> List[Tuple[str, str, str]]:
        return _rows(capture([python, script, "packs", "--root", str(repo), "--ids",
                              ",".join(ids), "--list"], repo))

    keep = []
    for pack_id in wanted:
        if pack_id not in available:
            notes.append(f"pack {pack_id!r} is not in this version's tree — left as installed")
            continue
        why = _killed(available[pack_id])
        if why:
            notes.append(f"pack {pack_id!r} not installed: {why}")
            continue
        keep.append(pack_id)
    if not keep:
        return []
    resolved = _resolve(keep)
    # Every pack of the resolved set (the dependencies too) is checked; a pack
    # whose closure holds a killed pack is not installed either.
    dead = {r[0]: why for r in resolved if (why := _killed(r))}
    if not dead:
        return resolved
    for pack_id, why in sorted(dead.items()):
        notes.append(f"pack {pack_id!r} not installed: {why}")
    survivors = []
    for pack_id in keep:
        bad = sorted({r[0] for r in _resolve([pack_id])} & set(dead))
        if bad:
            if pack_id not in dead:
                notes.append(f"pack {pack_id!r} not installed: it depends on killed "
                             f"pack(s) {', '.join(bad)}")
            continue
        survivors.append(pack_id)
    return _resolve(survivors) if survivors else []


def _rows(text: str) -> List[Tuple[str, str, str]]:
    out = []
    for line in (text or "").splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[0] and parts[2]:
            out.append((parts[0], parts[1], parts[2]))
    return out


def pack_commands(python: str, rows: List[Tuple[str, str, str]], *,
                  editable: bool, hashed: bool = True) -> List[List[str]]:
    """Step 2 for each pack of an OLD tree: the pack itself, ``--no-deps
    --no-build-isolation`` (its deps came hashed in step 1)."""
    flags = ["--no-deps", "--no-build-isolation"] if hashed else []
    return [[python, "-m", "pip", "install", *flags, *(["-e", d] if editable else [d])]
            for _, _, d in rows]
