"""Retire the separate first-party pack distributions of the 1.1.0-dev layout
(067, one install) — METADATA ONLY.

The first-party packs ship INSIDE the ``polyrob`` distribution. An environment
that installed them the old way keeps, per pack, a second provider of the
``polyrob.packs`` entry points and a stale ``polyrob==1.1.*`` pin. THE list is
``core.packs.index.RETIRED_DISTS``; this is the ONE helper that acts on it —
``install.sh``, ``scripts/deploy_dependency_cleanup.py --retire-pack-dists``
(the deploy), ``polyrob update`` and ``python -m migrations.migrate upgrade``
(the step an update started from an OLDER updater still runs from the target
tree) all call it.

⚠️ Never ``pip uninstall``: a NON-editable old pack wheel's RECORD lists the
``polyrob_<id>/…`` files the bundled ``polyrob`` wheel now owns, and pip would
delete them. :func:`retire` removes, for an allowlisted dist only:

* its ``.dist-info`` directory (must sit directly in the dist's site dir);
* from its RECORD, only top-level ``__editable__*.pth`` / ``__editable___*_finder.py``
  files and a top-level ``.pth`` whose content points at an old ``packs/<dist>``
  source dir — never a file under an import package, never a RECORD path that is
  absolute, holds ``..`` or leaves the site dir.

Guards (Codex final check): only a real dist-info directly in THIS
interpreter's site-packages (or user site), whose directory name is the retired
dist's own, is touched; only the retired dist's OWN editable hooks go; and
:func:`main` deletes nothing unless the ``polyrob`` dist already provides every
first-party pack entry point (else exit 2 with the remedy). Then :func:`verify`
(a fresh interpreter) requires exactly ONE provider, ``polyrob``, per pack.

⚠️ STDLIB ONLY and runnable as a script (``python core/packs/retire.py``): the
installer and the deploy call it with the target tree's file; it reads the list
from ``index.py`` beside it, by path, and never imports the ``core`` package.
"""
import os
import sys

# Run as a script, the interpreter puts core/packs/ first on sys.path; nothing
# there may shadow the stdlib modules imported below.
if __name__ == "__main__" and sys.path and os.path.realpath(sys.path[0] or ".") == \
        os.path.dirname(os.path.realpath(__file__)):
    sys.path.pop(0)

import importlib.util  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from importlib import metadata  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import List, Optional  # noqa: E402

GROUP = "polyrob.packs"
REMEDY = ("reinstall polyrob into this environment (a source tree: pip install --no-deps "
          "--no-build-isolation -e .), then run core/packs/retire.py again")
PRECONDITION_REMEDY = ("reinstall polyrob first (pip install -e . / pip install -U polyrob), "
                       "then python -m core.packs.retire")


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name or "")).lower()


def retired_names() -> frozenset:
    """``RETIRED_DISTS`` from ``index.py`` beside this file (no ``core`` import)."""
    path = Path(__file__).resolve().parent / "index.py"
    spec = importlib.util.spec_from_file_location("_polyrob_pack_index", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return frozenset(module.RETIRED_DISTS)


def pack_ids(names: Optional[frozenset] = None) -> List[str]:
    """The first-party pack ids the retired names stood for (a retired dist name -> its pack id)."""
    return sorted(n.split("polyrob-", 1)[1] for n in (names or retired_names()))


def _sites() -> List[Path]:
    """This interpreter's site-packages directories (+ the user site), realpaths."""
    import site
    dirs = list(site.getsitepackages())
    try:
        dirs.append(site.getusersitepackages())
    except Exception:  # noqa: BLE001
        pass
    return [Path(os.path.realpath(d)) for d in dirs if d]


@dataclass
class Result:
    retired: List[str] = field(default_factory=list)     # dist names whose metadata went
    removed: List[str] = field(default_factory=list)     # every path removed
    errors: List[str] = field(default_factory=list)
    double_provided: List[str] = field(default_factory=list)   # pack ids with >1 provider

    @property
    def ok(self) -> bool:
        return not self.errors and not self.double_provided


def _safe_top_level(site: Path, rel: str) -> Optional[Path]:
    """*rel* (a RECORD path) as a file DIRECTLY in *site*, or None."""
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts or len(p.parts) != 1:
        return None
    target = site / p
    try:
        if target.resolve().parent != site.resolve():
            return None
    except OSError:
        return None
    return target


def _own_hook(name: str, dist_name: str) -> bool:
    """An editable hook that belongs to *dist_name* itself — never another dist's
    (``__editable__.polyrob-1.2.0.pth`` is polyrob's, not a retired dist's)."""
    u = re.escape(dist_name.replace("-", "_"))
    return bool(re.fullmatch(rf"__editable__\.{u}-\d[^/\\]*\.pth", name)
                or re.fullmatch(rf"__editable___{u}_\d[A-Za-z0-9_]*_finder\.py", name))


def _removable(site: Path, rel: str, dist_name: str) -> Optional[Path]:
    target = _safe_top_level(site, rel)
    if target is None or target.is_symlink():
        return None
    name = target.name
    if name.startswith("__editable__"):
        return target if _own_hook(name, dist_name) else None
    if name.endswith(".pth"):
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        if any(re.search(rf"[/\\]packs[/\\]{re.escape(dist_name)}([/\\]|$)", line.strip())
               for line in text.splitlines()):
            return target
    return None


def _contained(info: Path, sites: List[Path]) -> Optional[str]:
    """Why *info* may NOT be touched, or None: it must be a real (not symlinked)
    dist-info directory directly in one of *sites*."""
    if info.is_symlink() or not info.is_dir():
        return f"{info} is not a real dist-info directory"
    parent = Path(os.path.realpath(info.parent))
    if Path(os.path.realpath(info)).parent != parent:
        return f"{info} resolves outside its directory"
    if parent not in {Path(os.path.realpath(s)) for s in sites}:
        return f"{info} is not in a site-packages directory of this interpreter"
    return None


def _retire_one(dist, sites: List[Path], result: Result) -> None:
    name = _canon(dist.metadata["Name"])
    info = Path(getattr(dist, "_path", "") or "")
    dir_name = _canon(info.name[:-len(".dist-info")].rsplit("-", 1)[0]) \
        if info.name.endswith(".dist-info") else ""
    if dir_name != name:
        result.errors.append(f"{info} claims Name {name} but is not its dist-info; skipped")
        return
    why = _contained(info, sites)
    if why:
        result.errors.append(f"{name}: {why}; skipped, remove it by hand")
        return
    site = Path(os.path.realpath(info.parent))
    for rel in dist.files or ():
        target = _removable(site, str(rel), name)
        if target is not None and target.is_file():
            target.unlink()
            result.removed.append(str(target))
    shutil.rmtree(info)
    result.removed.append(str(info))
    result.retired.append(name)


def retire(distributions=None, sites: Optional[List[Path]] = None) -> Result:
    """Retire every installed allowlisted dist (metadata only) found directly in
    *sites* (default: this interpreter's). Never raises. The caller checks the
    precondition first (:func:`main`)."""
    names = retired_names()
    sites = _sites() if sites is None else sites
    result = Result()
    dists = list(metadata.distributions() if distributions is None else distributions)
    for dist in dists:
        try:
            if _canon(dist.metadata["Name"]) in names:
                _retire_one(dist, sites, result)
        except Exception as exc:  # noqa: BLE001 — named, per dist
            result.errors.append(f"{dist.metadata['Name']}: {type(exc).__name__}: {exc}")
    importlib.invalidate_caches()
    return result


_PROBE = r"""
import importlib.util, json
from importlib import metadata
eps = {}
for ep in metadata.entry_points(group=%r):
    name = (ep.dist.metadata["Name"] if ep.dist is not None else "") or ""
    eps.setdefault(ep.name, []).append([name, ep.value])
out = {"eps": eps, "missing": []}
for pid, provs in eps.items():
    for dist, value in provs:
        if dist.lower() == "polyrob":
            top = value.partition(":")[0].split(".")[0]
            if importlib.util.find_spec(top) is None:
                out["missing"].append(top)
print(json.dumps(out))
""" % GROUP


def _probe(python: Optional[str] = None) -> dict:
    out = subprocess.run([python or sys.executable, "-I", "-c", _PROBE],
                         capture_output=True, text=True, check=True, timeout=120)
    return json.loads(out.stdout.strip().splitlines()[-1])


def polyrob_provides_all(data: dict) -> List[str]:
    """The first-party pack ids the ``polyrob`` dist does NOT provide (empty = all)."""
    return [pid for pid in pack_ids()
            if "polyrob" not in [_canon(d) for d, _ in data["eps"].get(pid, [])]]


def verify(result: Result, python: Optional[str] = None) -> Result:
    """In a FRESH interpreter: each first-party pack id has EXACTLY one provider,
    ``polyrob`` (none is a failure), and its package resolves."""
    try:
        data = _probe(python)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"could not verify the pack entry points: {exc}")
        return result
    for pid in pack_ids():
        dists = [_canon(d) for d, _ in data["eps"].get(pid, [])]
        if len(dists) > 1:
            result.double_provided.append(pid)
        elif dists != ["polyrob"]:
            result.errors.append(f"pack {pid!r} has no polyrob provider "
                                 f"({', '.join(dists) or 'none'})")
    for top in data["missing"]:
        result.errors.append(f"pack package {top} does not resolve")
    return result


def main(argv: Optional[List[str]] = None) -> int:
    """Precondition (nothing is deleted without it): the ``polyrob`` dist already
    provides every first-party pack entry point. Then retire, then verify."""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        missing = polyrob_provides_all(_probe())
    except Exception as exc:  # noqa: BLE001
        print(f"polyrob: could not read the pack entry points ({exc}); nothing retired; "
              f"remedy: {PRECONDITION_REMEDY}", file=sys.stderr)
        return 2
    if missing:
        names = retired_names()
        present = any(_canon(d.metadata["Name"]) in names for d in metadata.distributions())
        print(f"polyrob: this polyrob does not provide pack(s) {', '.join(missing)}; nothing "
              f"retired; remedy: {PRECONDITION_REMEDY}", file=sys.stderr)
        # With a retired dist still installed, pack loading is broken: 2 (migrate
        # upgrade fails). Without one there is nothing to double-provide: 1.
        return 2 if present else 1
    result = retire()
    if "--no-verify" not in argv:
        verify(result)
    for name in result.retired:
        print(f"retired pack distribution {name} (its pack ships inside polyrob; "
              f"metadata only, no pack file removed)")
    for pid in result.double_provided:
        print(f"polyrob: pack {pid!r} still has a second provider of its entry point; "
              f"remedy: {REMEDY}", file=sys.stderr)
    for err in result.errors:
        print(f"polyrob: {err}; remedy: {REMEDY}", file=sys.stderr)
    if result.double_provided:
        return 2
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
