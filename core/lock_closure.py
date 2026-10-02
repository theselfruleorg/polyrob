"""066 P1 — the hashed dependency closure of a set of roots, read from the lock.

``requirements.lock`` is compiled with ``--generate-hashes`` (066 D2). pip's hash
mode cannot hash an editable project ("there is no single file to hash"), so every
install is TWO steps:

1. ``pip install --require-hashes -r <deps>`` — ``<deps>`` is what this module
   writes: the lock entries (verbatim: pin, marker, every ``--hash``) of the
   transitive closure of the project's base dependencies plus the chosen extras;
2. ``pip install --no-deps --no-build-isolation [-e] <project>`` — the project
   itself; its build backend (``setuptools``) was installed hashed in step 1.

The lazy closures (``core/lazy_closures/<feature>.txt``) use the same reader. The
first-party packs ship INSIDE the project (067, one install); choosing a pack
(``--packs``) chooses the extra its ``pack.toml`` names (x -> twitter, ...).

⚠️ STDLIB ONLY and no ``core`` import: ``install.sh`` and ``scripts/deploy_prod.sh``
run this file as a script (``python core/lock_closure.py deps …``) BEFORE the
project is installed.

The graph comes from uv's ``# via`` comments (child -> its parents), inverted. That
is over-inclusive in one direction only: a package's children that only one of its
own extras needs are included too. Over-inclusion installs a pinned, hashed
package that nothing imports; it never leaves a needed one out.
"""
from __future__ import annotations

import os
import sys

# Run as a script, the interpreter puts core/ first on sys.path, and core/ holds
# `copy/` and `logging.py` — which would shadow the stdlib for every import below.
if __name__ == "__main__" and sys.path and os.path.realpath(sys.path[0] or ".") == \
        os.path.dirname(os.path.realpath(__file__)):
    sys.path.pop(0)

import hashlib  # noqa: E402
import re  # noqa: E402
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

#: The root node uv writes for a dependency declared by pyproject.toml.
_PROJECT = "polyrob"
#: The project's build backend. Always in a closure used to install the project,
#: so step 2 can run with ``--no-build-isolation`` (no unhashed download).
BUILD_ROOTS = ("setuptools",)
#: Extras that only list other extras (``all = ["polyrob[...]"]``) and ``dev``.
_META_EXTRAS = frozenset({"all"})

_REQ_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)")
_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_SELF_EXTRAS_RE = re.compile(r"^\s*polyrob\[([^\]]+)\]", re.I)


def canon(name: str) -> str:
    """PEP 503 normal form: ``Jinja2`` -> ``jinja2``, ``python_docx`` -> ``python-docx``."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class Entry:
    name: str            # canonical
    version: str
    block: str           # the verbatim requirement block: pin line + hash lines, no comments
    hashes: List[str] = field(default_factory=list)


@dataclass
class Lock:
    entries: Dict[str, List[Entry]]      # canonical name -> 1+ entries (marker forks)
    parents: Dict[str, Set[str]]         # canonical name -> canonical parents ("polyrob" = pyproject)
    digest: str                          # sha256 of the lock text, first 16 hex

    def children(self) -> Dict[str, Set[str]]:
        out: Dict[str, Set[str]] = {}
        for child, parents in self.parents.items():
            for parent in parents:
                out.setdefault(parent, set()).add(child)
        return out


def lock_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def parse_lock(text: str) -> Lock:
    """Parse uv's ``pip compile --generate-hashes`` output."""
    entries: Dict[str, List[Entry]] = {}
    parents: Dict[str, Set[str]] = {}
    current: Optional[Entry] = None
    in_via = False
    block_lines: List[str] = []

    def _close():
        nonlocal current, block_lines
        if current is not None:
            current.block = "\n".join(block_lines).rstrip(" \\") + "\n"
            entries.setdefault(current.name, []).append(current)
        current, block_lines = None, []

    for raw in text.splitlines():
        if not raw.strip():
            continue
        if not raw.startswith((" ", "#")):
            _close()
            in_via = False
            m = _REQ_RE.match(raw)
            if not m:
                raise ValueError(f"unparseable lock line: {raw!r}")
            current = Entry(name=canon(m.group(1)), version=m.group(2), block="")
            block_lines = [raw]
            continue
        stripped = raw.strip()
        if stripped.startswith("--hash=") and current is not None:
            current.hashes.append(stripped.rstrip(" \\").split("=", 1)[1])
            block_lines.append(raw)
            continue
        if stripped.startswith("# via"):
            rest = stripped[len("# via"):].strip()
            in_via = not rest
            if rest and current is not None:
                parents.setdefault(current.name, set()).add(_parent(rest))
            continue
        if in_via and stripped.startswith("#") and current is not None:
            parents.setdefault(current.name, set()).add(_parent(stripped.lstrip("#").strip()))
            continue
    _close()
    return Lock(entries=entries, parents=parents, digest=lock_digest(text))


def _parent(text: str) -> str:
    # "polyrob (pyproject.toml)" -> "polyrob"; "-r requirements.in" never occurs here.
    m = _NAME_RE.match(text)
    return canon(m.group(1)) if m else canon(text)


def closure(lock: Lock, roots: Iterable[str]) -> Set[str]:
    """Every lock entry reachable from *roots* (canonical names), roots included.
    A root the lock does not pin raises — a closure must never silently drop one."""
    children = lock.children()
    seen: Set[str] = set()
    stack = [canon(r) for r in roots]
    for r in stack:
        if r not in lock.entries:
            raise KeyError(f"{r} is not pinned in the lock")
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        stack.extend(c for c in children.get(name, ()) if c not in seen and c in lock.entries)
    return seen


def render(lock: Lock, names: Iterable[str], *, header: str = "") -> str:
    """The hashed requirements text for *names*, sorted, every marker fork kept."""
    out = [header.rstrip("\n") + "\n"] if header else []
    for name in sorted(set(names)):
        for entry in lock.entries[name]:
            out.append(entry.block)
    return "".join(out)


# --- pyproject roots ------------------------------------------------------------ #

def _load_pyproject(path: Path) -> dict:
    import tomllib
    with Path(path).open("rb") as fh:
        return tomllib.load(fh)["project"]


def _names(specs: Iterable[str]) -> Set[str]:
    out = set()
    for spec in specs:
        m = _NAME_RE.match(spec)
        if m:
            out.add(canon(m.group(1)))
    return out


def expand_extras(project: dict, extras: Iterable[str]) -> List[str]:
    """``all`` -> the extras it lists; unknown extra raises (a typo is not an empty set)."""
    declared = project.get("optional-dependencies", {})
    out: List[str] = []
    todo = [e.strip().lower() for e in extras if e and e.strip()]
    while todo:
        extra = todo.pop(0)
        if extra in out:
            continue
        if extra not in declared:
            raise KeyError(f"unknown extra {extra!r}; declared: {', '.join(sorted(declared))}")
        nested = [m.group(1) for s in declared[extra] if (m := _SELF_EXTRAS_RE.match(s))]
        if nested:
            for group in nested:
                todo.extend(x.strip() for x in group.split(","))
            if extra in _META_EXTRAS:
                continue
        out.append(extra)
    return out


def project_roots(project: dict, extras: Iterable[str], *, build: bool = True) -> Set[str]:
    """Base dependencies + every chosen extra's dependencies (+ the build backend)."""
    declared = project.get("optional-dependencies", {})
    roots = _names(project.get("dependencies", ()))
    for extra in expand_extras(project, extras):
        roots |= {n for n in _names(declared[extra]) if n != _PROJECT}
    if build:
        roots |= set(BUILD_ROOTS)
    return roots


def project_deps(lock_text: str, pyproject: Path, extras: Iterable[str],
                 packs: str = "") -> str:
    """Step 1 of an install: the hashed closure of polyrob[extras], plus the
    extras of the chosen first-party *packs* (``all``, ``none`` or a comma list
    of pack ids; each maps to the extra its ``pack.toml`` names)."""
    lock = parse_lock(lock_text)
    project = _load_pyproject(pyproject)
    all_packs = first_party_packs(Path(pyproject).parent)
    chosen = select_packs(all_packs, packs)
    extras = expand_extras(project, list(extras) + pack_extras(all_packs, chosen))
    roots = project_roots(project, extras)
    names = closure(lock, roots)
    with_packs = f" (packs [{','.join(p.id for p in chosen)}])" if chosen else ""
    header = (f"# 066 P1: hashed closure of polyrob[{','.join(extras)}]{with_packs} from "
              f"requirements.lock (digest {lock.digest}); written by core/lock_closure.py — "
              f"do not edit\n")
    return render(lock, names, header=header)


# --- first-party packs (067, one install) -------------------------------------- #
#
# The first-party packs ship INSIDE the polyrob distribution: each is a
# ``polyrob.packs`` entry point of the core pyproject whose package lives at
# ``packs/<dir>/<package>/`` with a ``pack.toml``. Their SDKs are NOT in the base
# install: each pack.toml names the core extra that carries them (``extra =``).
# So "install pack x" means "install polyrob[twitter]" — there is no second
# distribution to build, pin, upload or install.

PACKS_DIR = "packs"
PACK_ENTRY_GROUP = "polyrob.packs"


@dataclass(frozen=True)
class Pack:
    id: str                  # the entry-point name (``x``, ``discovery``)
    extra: str               # the core extra carrying its SDKs (``twitter``; "" = none)
    path: str                # POSIX path of the pack's root, from the repo root (``packs/x``)
    requires: tuple = ()     # ``requires_packs`` of its pack.toml


def first_party_packs(root: Path) -> List[Pack]:
    """Every ``polyrob.packs`` entry point of ``<root>/pyproject.toml``, with its
    ``pack.toml``, sorted by id. A pack that cannot be read raises: a pack that
    cannot be read is not an absent one."""
    import tomllib
    root = Path(root)
    with (root / "pyproject.toml").open("rb") as fh:
        project = tomllib.load(fh)["project"]
    eps = (project.get("entry-points") or {}).get(PACK_ENTRY_GROUP) or {}
    out: List[Pack] = []
    for pack_id, ref in sorted(eps.items()):
        package = str(ref).partition(":")[0].split(".", 1)[0]
        found = sorted((root / PACKS_DIR).glob(f"*/{package}/pack.toml"))
        if len(found) != 1:
            raise ValueError(f"pack {pack_id!r} ({ref}): expected one "
                             f"{PACKS_DIR}/<dir>/{package}/pack.toml, found {len(found)}")
        with found[0].open("rb") as fh:
            manifest = tomllib.load(fh)
        if manifest.get("id") != pack_id:
            raise ValueError(f"{found[0]}: id {manifest.get('id')!r} differs from the "
                             f"entry point name {pack_id!r}")
        extra = str(manifest.get("extra", "") or "")
        declared = project.get("optional-dependencies", {})
        if extra and extra not in declared:
            raise ValueError(f"{found[0]}: extra {extra!r} is not a polyrob extra")
        out.append(Pack(id=str(pack_id), extra=extra,
                        path=found[0].parent.parent.relative_to(root).as_posix(),
                        requires=tuple(manifest.get("requires_packs", ()) or ())))
    return out


def select_packs(packs: List[Pack], ids) -> List[Pack]:
    """``all`` -> every pack; ``""``/``none`` -> none; else the comma list (or
    iterable) of ids. An unknown id raises — a typo is not an empty set."""
    if isinstance(ids, str):
        wanted = [i.strip() for i in ids.replace(" ", ",").split(",") if i.strip()]
    else:
        wanted = [str(i).strip() for i in ids if str(i).strip()]
    if not wanted or wanted == ["none"]:
        return []
    if "all" in wanted:
        return list(packs)
    by_id = {p.id: p for p in packs}
    unknown = [i for i in wanted if i not in by_id]
    if unknown:
        raise KeyError(f"unknown pack {', '.join(unknown)}; this tree has: "
                       f"{', '.join(sorted(by_id)) or '(no packs)'}")
    return [by_id[i] for i in dict.fromkeys(wanted)]


def install_order(packs: List[Pack], chosen: List[Pack]) -> List[Pack]:
    """*chosen* plus every first-party pack they require, dependencies first."""
    by_id = {p.id: p for p in packs}
    out: List[Pack] = []

    def visit(p: Pack, trail=()):
        if p in out:
            return
        if p.id in trail:
            raise ValueError(f"first-party packs require each other in a cycle: {p.id}")
        for dep in p.requires:
            if dep not in by_id:
                raise ValueError(f"pack {p.id!r} requires pack {dep!r}, which this tree lacks")
            visit(by_id[dep], trail + (p.id,))
        out.append(p)

    for p in chosen:
        visit(p)
    return out


def pack_extras(packs: List[Pack], chosen: List[Pack]) -> List[str]:
    """The core extras of *chosen* (and of the packs they require), in order, once."""
    return list(dict.fromkeys(p.extra for p in install_order(packs, chosen) if p.extra))


def lock_command(root: Path) -> List[str]:
    """The ONE relock command (run from *root*); ``requirements.lock``'s header
    records it and the drift test compares the two."""
    import tomllib
    root = Path(root)
    with (root / "pyproject.toml").open("rb") as fh:
        spec = tomllib.load(fh)["project"]["requires-python"]
    m = re.search(r">=\s*(\d+\.\d+)", spec)
    if not m:
        raise ValueError(f"requires-python has no >= floor: {spec!r}")
    return ["uv", "pip", "compile", "pyproject.toml", "--all-extras", "--universal",
            "--python-version", m.group(1), "--generate-hashes", "-o", "requirements.lock"]


# --- generated files (scripts/gen_lazy_closures.py; drift-tested) ------------- #

#: Pins whose release has NO wheel for linux x86_64 / cp312 (checked against PyPI
#: with ``scripts/gen_lazy_closures.py --check-wheels``). A closure that contains
#: one is written ``wheel: false`` and a wheel-only box refuses it with the remedy
#: "add the extra at deploy". Empty on 2026-09-23: every lazy row has wheels.
NO_WHEEL: frozenset = frozenset()


def render_feature(lock: Lock, feature: str, roots: Iterable[str]) -> str:
    """``core/lazy_closures/<feature>.txt``: the FULL transitive closure of the
    row, verbatim from the lock. The installer skips, at install time, every pin
    the running environment already has at the same version — exact, where a
    static "minus the base install" would have to guess what the base is."""
    names = closure(lock, roots)
    wheel = not any(n in NO_WHEEL for n in names)
    header = (f"# 066 P1 lazy closure — GENERATED by scripts/gen_lazy_closures.py from "
              f"requirements.lock; do not edit\n"
              f"# feature: {feature}\n"
              f"# lock-digest: {lock.digest}\n"
              f"# wheel: {'true' if wheel else 'false'}\n")
    return render(lock, names, header=header)


REQUIREMENTS_TXT_EXTRAS_RE = re.compile(r"^# extras: ([a-z0-9,\-]+)\s*$", re.M)


def render_requirements_txt(lock_text: str, pyproject: Path, extras: Iterable[str]) -> str:
    """``requirements.txt``: the server install set (= PROD_EXTRAS), hashed."""
    extras = list(extras)
    lock = parse_lock(lock_text)
    project = _load_pyproject(pyproject)
    roots = project_roots(project, expand_extras(project, extras))
    names = closure(lock, roots)
    header = (
        "# Server install set — the SAME pyproject extras scripts/deploy_prod.sh\n"
        "# installs (PROD_EXTRAS), HASHED (066 P1 / D2). The first-party packs ship inside\n"
        "# polyrob; their SDKs are the twitter / anysite / crypto extras below.\n"
        "# GENERATED by scripts/gen_lazy_closures.py from requirements.lock; do not edit\n"
        "# by hand — edit the extras line and rerun.\n"
        "#\n"
        f"# extras: {','.join(extras)}\n"
        f"# lock-digest: {lock.digest}\n"
        "#\n"
        "# docs (PDF/DOCX parsing) and media (invoice cards, GIFs) are NOT\n"
        "# optional for a production box. gemini/anthropic are deliberately absent: prod runs\n"
        "# OpenRouter (058 T1.7); a provider SDK arrives through the trusted lazy install.\n"
        "#\n"
        "# Install from the repo root in TWO steps (pip's hash mode cannot hash the project):\n"
        "#     pip install --require-hashes -r requirements.txt\n"
        "#     pip install --no-deps --no-build-isolation -e .\n"
        "# A lighter install picks its own extras (or packs, which map to extras):\n"
        "#     python core/lock_closure.py deps --lock requirements.lock \\\n"
        "#         --pyproject pyproject.toml --extras server --packs x -o deps.txt\n")
    return render(lock, names, header=header)


# --- CLI ----------------------------------------------------------------------- #

def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="lock_closure",
                                 description="Write the hashed dependency closure of polyrob[extras].")
    sub = ap.add_subparsers(dest="cmd", required=True)
    deps = sub.add_parser("deps", help="hashed deps of the project + extras (step 1 of an install)")
    deps.add_argument("--lock", required=True)
    deps.add_argument("--pyproject", required=True)
    deps.add_argument("--extras", default="", help="comma-separated extras, e.g. server,docs")
    deps.add_argument("--packs", default="",
                      help="first-party packs whose SDK extras to add: all, none or ids (x,discovery)")
    deps.add_argument("-o", "--output", required=True)
    sub.add_parser("digest", help="print the lock digest").add_argument("--lock", required=True)
    pk = sub.add_parser("packs", help="print the chosen first-party pack ids, dependencies first")
    pk.add_argument("--root", required=True, help="the source tree (holds pyproject.toml, packs/)")
    pk.add_argument("--ids", default="all", help="all, none or a comma list of pack ids")
    pk.add_argument("--list", action="store_true", help="print `id extra dir` rows instead")
    args = ap.parse_args(argv)
    if args.cmd == "digest":
        print(lock_digest(Path(args.lock).read_text(encoding="utf-8")))
        return 0
    if args.cmd == "packs":
        try:
            packs = first_party_packs(Path(args.root))
            chosen = install_order(packs, select_packs(packs, args.ids))
        except (KeyError, ValueError, OSError) as exc:
            print(f"lock_closure: {exc}", file=sys.stderr)
            return 2
        for p in chosen:
            print(f"{p.id} {p.extra or '-'} {Path(args.root) / p.path}" if args.list
                  else p.id)
        return 0
    try:
        text = project_deps(Path(args.lock).read_text(encoding="utf-8"), Path(args.pyproject),
                            [e for e in args.extras.split(",") if e.strip()], args.packs)
    except (KeyError, ValueError, OSError) as exc:
        print(f"lock_closure: {exc}", file=sys.stderr)
        return 2
    Path(args.output).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
