"""The ONE definition of which files a shipped tree contains.

Two rails ask "what is in this tree":

- ``tools/hf_deploy/digest.py::compute_workspace_digest`` — the sha256 recorded
  as the TESTED tree (``ship == tested``);
- ``core/app_service/snapshot.py::snapshot_tree`` — the bytes that actually
  reach ``/app`` inside an app container.

They used to filter DIFFERENT trees (the digest pruned ``coding_snapshots`` and
``node_modules``; the snapshot pruned the venv/cache dirs, symlinks and
credential files), so the recorded digest did not identify the shipped bytes: an
edit under a directory one skipped and the other shipped moved the running app
without moving its "tested" fingerprint. Both now walk with
:func:`walk_shippable`, so the digest is computed over exactly the tree the
snapshot would ship.

``node_modules`` is deliberately NOT skipped: an app container mounts ``/app``
read-only with egress denied by default, so vendored dependencies are the only
way a Node app runs — they ship, therefore they must be hashed.
"""
import os
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

#: Build/vcs/cache noise pruned at ANY depth. Never ships, never hashed.
SKIP_DIRS = frozenset({
    ".git", "coding_snapshots", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".tox", ".venv", "venv", ".pylibs",
})

SKIP_SYMLINK = "symlink"
SKIP_SPECIAL = "special"
SKIP_CREDENTIAL = "credential"


def _is_credential_file(full: str) -> bool:
    """``secret_guard`` says so. Fail-CLOSED: an unreadable/blown-up scanner
    refuses the file rather than shipping a possible credential."""
    try:
        from core.security.secret_guard import is_credential_file
    except Exception:  # pragma: no cover - import failure is not a ship signal
        return True
    try:
        return bool(is_credential_file(Path(full)))
    except Exception:
        return True


def file_skip_reason(full: str) -> Optional[str]:
    """Why *full* never ships, or ``None`` when it does."""
    if os.path.islink(full):
        return SKIP_SYMLINK
    if not os.path.isfile(full):
        return SKIP_SPECIAL
    if _is_credential_file(full):
        return SKIP_CREDENTIAL
    return None


def _rel(rel_root: str, name: str) -> str:
    joined = name if rel_root in (".", "") else os.path.join(rel_root, name)
    return os.path.normpath(joined).replace(os.sep, "/")


def walk_shippable(root: str, skipped: Optional[List[str]] = None) -> Iterator[Tuple[str, str]]:
    """Yield ``(rel, full)`` for every file a shipped tree contains.

    *rel* is always ``/``-separated. Order is deterministic (sorted names at
    each level); a caller that needs a global order sorts the collected
    ``rel``s. When *skipped* is a list, one human-readable line is appended per
    refusal (``"pkg/.git/"``, ``"link (symlink)"``, ``".env (credential)"``) so
    a result can say exactly what the tree does NOT carry.
    """
    root_real = os.path.realpath(root)
    for dirpath, dirnames, filenames in os.walk(root_real):
        rel_root = os.path.relpath(dirpath, root_real)
        kept = []
        for d in sorted(dirnames):
            full = os.path.join(dirpath, d)
            rel = _rel(rel_root, d)
            if d in SKIP_DIRS:
                if skipped is not None:
                    skipped.append(rel + "/")
                continue
            if os.path.islink(full):
                if skipped is not None:
                    skipped.append(rel + f" ({SKIP_SYMLINK})")
                continue
            kept.append(d)
        dirnames[:] = kept
        for n in sorted(filenames):
            full = os.path.join(dirpath, n)
            rel = _rel(rel_root, n)
            reason = file_skip_reason(full)
            if reason is not None:
                if skipped is not None:
                    skipped.append(f"{rel} ({reason})")
                continue
            yield rel, full
