"""Uninstall third-party distributions polyrob no longer depends on and whose
presence breaks the environment.

THE list is :data:`RETIRED_THIRD_PARTY` (name → why). Dropping a pyproject row
uninstalls nothing: an existing venv keeps the old dist, and its pins then fail
``pip check`` — which every update path runs (the deploy, and the verify step of
``polyrob update``, often the OLDER updater). The callers:

* ``scripts/deploy_dependency_cleanup.py`` (the deploy, before its ``pip check``);
* ``python -m migrations.migrate upgrade`` (the one step an older ``polyrob
  update`` runs from the TARGET tree before its ``pip check`` verify);
* ``install.sh`` (a re-run over an existing venv).

A dist that another installed dist still requires (outside an ``extra``
marker) is left in place and named. STDLIB ONLY and runnable as a script
(``python core/retired_deps.py``): the deploy and ``install.sh`` load it by path.
"""
from __future__ import annotations

import re
import subprocess
import sys
from importlib import metadata
from typing import Callable, Dict, Iterable, List, Optional, Tuple

#: name → why it is retired.
RETIRED_THIRD_PARTY: Dict[str, str] = {
    "tweepy": ("the X pack carries tweepy itself (polyrob_x/_vendor); the PyPI tweepy "
               "4.17.0 pins oauthlib<4, and oauthlib>=4.0.1 has the CVE fixes"),
}

_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requires(dist) -> List[str]:
    """Canonical names *dist* requires unconditionally of extras."""
    out = []
    for req in dist.requires or ():
        if ";" in req and "extra" in req.split(";", 1)[1]:
            continue
        m = _NAME.match(req)
        if m:
            out.append(_canon(m.group(1)))
    return out


def plan(distributions: Optional[Iterable] = None) -> Tuple[List[str], Dict[str, List[str]]]:
    """(to uninstall, kept → the installed dists that still require it)."""
    dists = list(metadata.distributions() if distributions is None else distributions)
    installed = {_canon(d.metadata["Name"]): d for d in dists if d.metadata["Name"]}
    remove, kept = [], {}
    for name in RETIRED_THIRD_PARTY:
        key = _canon(name)
        if key not in installed:
            continue
        users = sorted(_canon(d.metadata["Name"]) for k, d in installed.items()
                       if k != key and key in _requires(d))
        if users:
            kept[name] = users
        else:
            remove.append(name)
    return remove, kept


def _pip_uninstall(name: str) -> None:
    subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "-q", name], check=True)


def main(argv: Optional[List[str]] = None, *, distributions: Optional[Iterable] = None,
         uninstall: Callable[[str], None] = _pip_uninstall) -> int:
    """0 = nothing left to retire; 1 = a retired dist stays (named on stderr)."""
    remove, kept = plan(distributions)
    rc = 0
    for name in remove:
        try:
            uninstall(name)
            print(f"retired {name}: {RETIRED_THIRD_PARTY[name]}")
        except Exception as exc:  # noqa: BLE001 — named, never silent
            print(f"polyrob: could not uninstall {name} ({exc}); remedy: "
                  f"{sys.executable} -m pip uninstall -y {name}", file=sys.stderr)
            rc = 1
    for name, users in kept.items():
        print(f"polyrob: {name} is retired but still required by {', '.join(users)}; "
              f"left installed", file=sys.stderr)
        rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
