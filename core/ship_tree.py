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


def tree_digest(root: str) -> str:
    """Deterministic sha256 over exactly the files :func:`walk_shippable` yields.

    Sorted relative paths + file bytes. The ONE digest: the ship==tested gate
    (``tools/hf_deploy/digest.py``) records it, and the app supervisor recomputes
    it over its own snapshot before it publishes anything."""
    import hashlib
    root = os.path.abspath(root)
    paths = {rel: full for rel, full in walk_shippable(root)}
    h = hashlib.sha256()
    for rel in sorted(paths):
        h.update(rel.encode("utf-8", errors="replace"))
        h.update(b"\x00")
        try:
            with open(paths[rel], "rb") as f:
                h.update(f.read())
        except OSError:
            pass  # a file that vanished mid-walk contributes no bytes, not a crash
        h.update(b"\x00")
    return h.hexdigest()


# --- the tree the last green test saw (ship == tested, by content) -----------
#
# The ledger-order gate (``agents.task.runtime.edit_verify``) only knows the
# ``coding`` edit verbs; a host-shell ``sed -i``, a background job or a
# ``run_code`` cell after a green ``run_tests`` left it blind, and the digest the
# deploy recorded then described a tree no test had run against. ``run_tests``
# records the tree it saw here; the gate compares the tree it is about to ship.
# Process-local (a restart forgets, and the gate falls back to the ledger rule).

#: A tree bigger than this is not recorded (the gate falls back to the ledger).
_MANIFEST_MAX_FILES = 50_000
_TESTED_MAX_SESSIONS = 64
_TESTED: "dict[str, tuple[str, dict]]" = {}


def tree_manifest(root: str) -> "Optional[dict]":
    """``{rel: (size, mtime_ns)}`` over :func:`walk_shippable`, or None when the
    tree is too big to hold."""
    out = {}
    for rel, full in walk_shippable(root):
        try:
            st = os.stat(full)
        except OSError:
            continue
        out[rel] = (st.st_size, st.st_mtime_ns)
        if len(out) > _MANIFEST_MAX_FILES:
            return None
    return out


def record_tested_tree(session_id: str, root: str) -> None:
    """Remember the tree a GREEN test run just finished against."""
    if not session_id or not root:
        return
    manifest = tree_manifest(root)
    _TESTED.pop(session_id, None)
    if manifest is None:
        return
    _TESTED[session_id] = (os.path.realpath(root), manifest)
    while len(_TESTED) > _TESTED_MAX_SESSIONS:
        _TESTED.pop(next(iter(_TESTED)))


def untested_changes(session_id: str, ship_root: str) -> Optional[List[str]]:
    """Files under *ship_root* that differ from what the last green test saw
    (added, removed, or changed), sorted; ``[]`` when none. ``None`` when there
    is no comparable record (none kept, or *ship_root* is outside the tested
    root) — the caller then keeps its ledger rule."""
    rec = _TESTED.get(session_id or "")
    if rec is None:
        return None
    tested_root, manifest = rec
    ship = os.path.realpath(ship_root)
    if ship != tested_root and not ship.startswith(tested_root + os.sep):
        return None
    sub = os.path.relpath(ship, tested_root).replace(os.sep, "/")
    prefix = "" if sub == "." else sub + "/"
    current = tree_manifest(ship)
    if current is None:
        return None
    current = {prefix + rel: v for rel, v in current.items()}
    tested = {r: v for r, v in manifest.items() if r.startswith(prefix)}
    return sorted(r for r in current.keys() | tested.keys() if current.get(r) != tested.get(r))
