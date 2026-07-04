"""Install-method detection — the routing decision for `polyrob update`.

The fetch/install strategy is a function of *how* POLYROB was installed. Detection is
**fail-safe**: an unrecognised layout resolves to ``unknown`` so the update engine
refuses to mutate and prints manual steps rather than guessing.

The core, :func:`classify_install`, takes explicit probe inputs so it is fully unit
testable without a real install; :func:`detect_install` wires the live probes.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Install methods, in the order they are decided (first match wins).
DOCKER = "docker"
SYSTEMD = "systemd"
EDITABLE_GIT = "editable_git"
GIT = "git"
PIPX = "pipx"
PIP = "pip"
UNKNOWN = "unknown"

# Methods this session can safely self-update in place vs. must defer to a manager.
SELF_UPDATABLE = frozenset({SYSTEMD, EDITABLE_GIT, GIT, PIPX, PIP})
DEFER_TO_MANAGER = frozenset({DOCKER, UNKNOWN})


@dataclass(frozen=True)
class InstallContext:
    method: str
    package_dir: Path
    repo_root: Optional[Path]
    reason: str = ""

    @property
    def self_updatable(self) -> bool:
        return self.method in SELF_UPDATABLE


def _truthy(val: Optional[str]) -> bool:
    return str(val or "").strip().lower() in {"1", "true", "yes", "on"}


def find_git_root(start: Path) -> Optional[Path]:
    """Nearest ancestor (incl. ``start``) containing a ``.git`` entry, else None."""
    start = Path(start)
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _iter_polyrob_dists():
    """All installed distributions named ``polyrob`` (usually 0 or 1).

    Enumerating rather than ``distribution("polyrob")`` matters: when run from the
    source tree a stray ``polyrob.egg-info`` (no ``direct_url.json``) can be resolved
    FIRST and shadow the real ``.dist-info`` that records the editable flag.
    """
    try:
        from importlib.metadata import distributions

        out = []
        for dist in distributions():
            try:
                name = (dist.metadata.get("Name") or "").strip().lower()
            except Exception:
                name = ""
            if name == "polyrob":
                out.append(dist)
        return out
    except Exception:
        return []


def read_editable_flag(package_dir: Path) -> Optional[bool]:
    """Whether the installed ``polyrob`` dist is an editable install.

    Reads ``direct_url.json`` per PEP 610 across ALL polyrob dists so a shadowing
    egg-info can't hide the editable flag. Returns None when no dist carries a
    ``direct_url.json`` (not pip-installed, or legacy develop) so the caller can fall
    back to git probing.
    """
    for dist in _iter_polyrob_dists():
        try:
            raw = dist.read_text("direct_url.json")
        except Exception:
            raw = None
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        return bool(data.get("dir_info", {}).get("editable", False))
    return None


def _looks_like_pipx(package_dir: Path, env: os._Environ) -> bool:
    joined = str(package_dir).replace(os.sep, "/")
    if "/pipx/venvs/polyrob/" in joined:
        return True
    home = env.get("PIPX_HOME")
    return bool(home and str(Path(home)) in str(package_dir))


def classify_install(
    *,
    package_dir: Path,
    env: os._Environ,
    dockerenv_exists: bool,
    editable_flag: Optional[bool],
    git_root: Optional[Path],
    dist_present: bool,
) -> InstallContext:
    """Pure classifier — decides the method from explicit probe results."""
    package_dir = Path(package_dir)

    if dockerenv_exists or _truthy(env.get("POLYROB_IN_DOCKER")):
        return InstallContext(DOCKER, package_dir, git_root,
                              "container detected; update by rebuilding the image")

    under_opt = str(package_dir).startswith("/opt/polyrob")
    if env.get("INVOCATION_ID") or under_opt:
        return InstallContext(SYSTEMD, package_dir, git_root,
                              "systemd/server install")

    if git_root is not None:
        if editable_flag:
            return InstallContext(EDITABLE_GIT, package_dir, git_root,
                                  "editable install over a git checkout")
        return InstallContext(GIT, package_dir, git_root, "git checkout")

    if _looks_like_pipx(package_dir, env):
        return InstallContext(PIPX, package_dir, None, "pipx-managed venv")

    if dist_present:
        return InstallContext(PIP, package_dir, None, "pip wheel install")

    return InstallContext(UNKNOWN, package_dir, None,
                          "could not determine install method")


def _code_root() -> Path:
    # cli/update/detect.py -> repo/site-packages root (parent of cli/).
    return Path(__file__).resolve().parents[2]


def detect_install(env: Optional[os._Environ] = None,
                   package_dir: Optional[Path] = None) -> InstallContext:
    """Live detection wiring real probes into :func:`classify_install`."""
    env = env if env is not None else os.environ
    pkg = Path(package_dir) if package_dir else _code_root()

    editable = read_editable_flag(pkg)
    git_root = find_git_root(pkg)
    try:
        from importlib.metadata import distribution

        distribution("polyrob")
        dist_present = True
    except Exception:
        dist_present = False

    return classify_install(
        package_dir=pkg,
        env=env,
        dockerenv_exists=Path("/.dockerenv").exists(),
        editable_flag=editable,
        git_root=git_root,
        dist_present=dist_present,
    )
