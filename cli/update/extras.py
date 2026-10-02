"""Which extras THIS install has, so an update reinstalls the same shape (058).

``pip install -e .`` after a code swap drops every extra: an already-installed
package survives (pip never uninstalls), but a NEW dependency an extra gained
in the new version never arrives, and an unconstrained install can move a
pinned core package out from under a venv the deploy verified. So the update
carries ``.[<extras present>]`` and ``-c requirements.lock`` when the lock is
in the tree — the same two rules ``core/lazy_deps.py`` and
``scripts/deploy_prod.sh`` follow.

An extra is "present" when every distribution it declares is installed. The
declaration comes from ``pyproject.toml`` at the repo root when there is one
(a git / editable install — ⚠️ the dist's own metadata is STALE there: an
editable install's ``Provides-Extra`` only refreshes on reinstall, so it
cannot know about an extra added since), else from the installed dist's
``Requires-Dist`` markers (a wheel install has no pyproject).
"""
from __future__ import annotations

import importlib.metadata as _md
import re
from pathlib import Path
from typing import Dict, List, Optional, Set

#: Extras that are tooling or aliases, never a capability to carry across an update.
_META_EXTRAS = frozenset({"dev", "all"})
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")
_EXTRA_MARKER_RE = re.compile(r"""extra\s*==\s*['"]([A-Za-z0-9._-]+)['"]""")


def _norm(name: str) -> str:
    return name.lower().replace("_", "-")


def _dist_name(spec: str) -> str:
    m = _NAME_RE.match(spec.strip())
    return _norm(m.group(0) if m else spec)


def _dist():
    return _md.distribution("polyrob")


def _dist_present(name: str) -> bool:
    try:
        _md.distribution(name)
        return True
    except _md.PackageNotFoundError:
        return False


def declared_extras(repo_root: Optional[Path]) -> Dict[str, Set[str]]:
    """extra -> the distribution names it declares (meta extras excluded)."""
    out: Dict[str, Set[str]] = {}
    pyproject = Path(repo_root) / "pyproject.toml" if repo_root else None
    if pyproject is not None and pyproject.is_file():
        import tomllib
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        for extra, specs in (data.get("project", {}).get("optional-dependencies") or {}).items():
            if extra in _META_EXTRAS:
                continue
            names = {_dist_name(s) for s in specs if not _dist_name(s).startswith("polyrob")}
            out[extra] = names
        return out
    try:
        dist = _dist()
    except _md.PackageNotFoundError:
        return out
    for req in dist.requires or ():
        m = _EXTRA_MARKER_RE.search(req)
        if not m or m.group(1) in _META_EXTRAS:
            continue
        name = _dist_name(req.split(";", 1)[0])
        if name.startswith("polyrob"):
            continue
        out.setdefault(m.group(1), set()).add(name)
    return out


def installed_extras(repo_root: Optional[Path]) -> List[str]:
    """The extras whose every declared distribution is installed, sorted."""
    return sorted(extra for extra, names in declared_extras(repo_root).items()
                  if names and all(_dist_present(n) for n in names))


def lock_path(repo_root: Optional[Path]) -> Optional[Path]:
    if not repo_root:
        return None
    p = Path(repo_root) / "requirements.lock"
    return p if p.is_file() else None


def pip_target(repo_root: Optional[Path], extras: List[str], *, editable: bool) -> List[str]:
    """The tail of the pip install command: ``[-c <lock>] [-e] .[extras]``."""
    args: List[str] = []
    lock = lock_path(repo_root)
    if lock is not None:
        args += ["-c", str(lock)]
    spec = "." + (f"[{','.join(sorted(extras))}]" if extras else "")
    if editable:
        args += ["-e", spec]
    else:
        args.append(spec)
    return args


def install_commands(python: str, repo_root: Optional[Path], extras: List[str], *,
                     editable: bool, deps_file: Path,
                     packs: Optional[List[str]] = None) -> List[List[str]]:
    """066 P1 / D2: the hash-checked install of THIS tree, as argv lists.

    pip's hash mode cannot hash the project itself, so with a lock it is three
    commands: write the lock's closure of the extras (``core/lock_closure.py``,
    run from the NEW tree), install it ``--require-hashes``, then the project
    ``--no-deps --no-build-isolation``. A source build is allowed where a
    platform has no wheel (an OSS install; prod is wheel-only). Without a lock:
    the one unconstrained pip command, as before.

    067 P8: *packs* (first-party pack ids, see :mod:`cli.update.packs`) add their
    third-party dependencies to the closure (``--packs``); the packs themselves
    install afterwards (``cli.update.packs.pack_commands``). The flag is passed
    only when there are packs, so a tree whose resolver predates it still works.
    """
    lock = lock_path(repo_root)
    if lock is None:
        return [[python, "-m", "pip", "install", *pip_target(repo_root, extras, editable=editable)]]
    repo = Path(repo_root)
    project = ["-e", "."] if editable else ["."]
    return [
        [python, str(repo / "core" / "lock_closure.py"), "deps", "--lock", str(lock),
         "--pyproject", str(repo / "pyproject.toml"), "--extras", ",".join(sorted(extras)),
         *(["--packs", ",".join(packs)] if packs else []),
         "-o", str(deps_file)],
        [python, "-m", "pip", "install", "--require-hashes", "--prefer-binary", "-r", str(deps_file)],
        [python, "-m", "pip", "install", "--no-deps", "--no-build-isolation", *project],
    ]


def upgrade_spec(extras: List[str]) -> str:
    """What a pip/pipx user types: ``"polyrob[docs,server]"``, or bare ``polyrob``."""
    if not extras:
        return "polyrob"
    return f'"polyrob[{",".join(sorted(extras))}]"'
