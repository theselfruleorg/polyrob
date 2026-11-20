"""Restore manifest data only to operator-selected roots, through pinned directories."""
import json
import os
from pathlib import Path
import secrets
import shutil
import stat

from core.security.confined_write import confined_parent

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _read(directory, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("snapshot contains a linked or non-regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read(), stat.S_IMODE(info.st_mode) & 0o777
    finally:
        os.close(fd)


def _write(directory, name, data, mode):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 mode & 0o777, dir_fd=directory)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _target_dir_mode(destination, target):
    """The ``rwx`` bits the live dir has now (a shared 2770 ``skills/`` stays
    group-readable for the console and email units; a 0700 ``wallet/`` stays
    private), else 0700. Set at ``mkdir``: a later chmod would clear setgid."""
    try:
        return stat.S_IMODE(os.stat(target, dir_fd=destination, follow_symlinks=False).st_mode) & 0o777
    except OSError:
        return 0o700


def _copy_tree(source, destination, depth=0, mode=0o700):
    if depth > 32:
        raise OSError("snapshot directory nesting exceeds restore limit")
    for name in os.listdir(source):
        info = os.stat(name, dir_fd=source, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            src = os.open(name, _DIR, dir_fd=source)
            try:
                os.mkdir(name, mode, dir_fd=destination)
                dst = os.open(name, _DIR, dir_fd=destination)
                try:
                    _copy_tree(src, dst, depth + 1, mode)
                finally:
                    os.close(dst)
            finally:
                os.close(src)
        else:
            data, file_mode = _read(source, name)
            _write(destination, name, data, file_mode)


def _preflight(source, plans):
    """All or nothing on a read-only target (runtime-sandbox contract).

    Under the hardened unit a config file captured from the read-only code dir
    (``/opt/polyrob/config/.env*``) cannot be written back: EROFS came AFTER the
    databases were already replaced, and the rollback ended half done. A file
    whose bytes are already in place needs no write and is dropped; any other
    target whose directory is not writable refuses the WHOLE restore first."""
    kept, blocked = [], []
    for plan in plans:
        item, stored, target, _root = plan
        if item.kind == "file" and target.is_file():
            folder = os.open(stored.parts[0], _DIR, dir_fd=source)
            try:
                content, _ = _read(folder, stored.name)
            finally:
                os.close(folder)
            try:
                if target.read_bytes() == content:
                    continue
            except OSError:
                pass
        parent = target.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if not os.access(parent, os.W_OK):
            blocked.append(str(target))
        kept.append(plan)
    if blocked:
        raise OSError("snapshot destination(s) not writable here (read-only under the "
                      "service unit?): " + ", ".join(blocked) + " — nothing was restored; "
                      "restore as the owner of those paths")
    return kept


def restore(snapshot_dir, *, data_home, allowed_paths=()):
    from core.update_snapshot.snapshot import SnapshotManifest, SnapshotItem, DONE_MARKER, MANIFEST_NAME

    snapshot_dir = Path(snapshot_dir).absolute()
    home = Path(data_home).resolve()
    allowed = {Path(path).absolute() for path in allowed_paths}
    with confined_parent(snapshot_dir, snapshot_dir.parent) as (parent, name):
        source = os.open(name, _DIR, dir_fd=parent)
    try:
        info = os.fstat(source)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise OSError("snapshot must be owned by the restoring user and not group/world writable")
        try:
            _read(source, DONE_MARKER)
        except FileNotFoundError as exc:
            raise RuntimeError("refusing to restore incomplete snapshot (no DONE marker)") from exc
        raw, _ = _read(source, MANIFEST_NAME)
        data = json.loads(raw)
        items = [SnapshotItem(**item) for item in data.get("items", [])]
        keys = {k: data[k] for k in ("from_version", "to_version", "method", "created_at", "git_sha")}
        manifest = SnapshotManifest(**keys, items=items, label=data.get("label"),
                                    scope=data.get("scope", "full"))
        # Validate EVERY manifest destination before restoring the first item.
        plans, targets = [], set()
        for item in items:
            stored = Path(item.stored)
            target = Path(item.original)
            if (item.kind not in {"db", "file", "dir"} or not target.is_absolute()
                    or ".." in target.parts or stored.is_absolute() or ".." in stored.parts
                    or len(stored.parts) != 2
                    or stored.parts[0] != {"db": "db", "file": "config", "dir": "dirs"}[item.kind]):
                raise OSError("unsafe snapshot manifest path or kind")
            if target == home or target in targets:
                raise OSError("duplicate or root snapshot destination")
            if target.is_relative_to(home):
                root = home
            elif target in allowed:
                root = target.parent
            else:
                raise OSError("snapshot destination is outside the configured restore paths")
            targets.add(target)
            plans.append((item, stored, target, root))
        plans = _preflight(source, plans)
        for item, stored, target, root in plans:
            folder = os.open(stored.parts[0], _DIR, dir_fd=source)
            try:
                with confined_parent(target, root, create=True) as (destination, name):
                    temp = ".restore-" + secrets.token_hex(16)
                    if item.kind == "dir":
                        _restore_tree(folder, stored.name, destination, name, temp)
                    else:
                        content, mode = _read(folder, stored.name)
                        try:
                            _write(destination, temp, content, mode)
                            os.replace(temp, name, src_dir_fd=destination, dst_dir_fd=destination)
                        finally:
                            try:
                                os.unlink(temp, dir_fd=destination)
                            except FileNotFoundError:
                                pass
                        if item.kind == "db":
                            for suffix in ("-wal", "-shm"):
                                try:
                                    os.unlink(name + suffix, dir_fd=destination)
                                except FileNotFoundError:
                                    pass
            finally:
                os.close(folder)
        return manifest
    finally:
        os.close(source)


def _keep_live(destination, target, staged_fd):
    """The files a rollback never restores (``context.ROLLBACK_KEPT_FILES``): the live copy
    replaces the snapshot's in the staged tree, and a file that is gone stays gone."""
    from core.update_snapshot.context import rollback_kept_files
    kept = rollback_kept_files(target)
    if not kept:
        return
    try:
        live = os.open(target, _DIR, dir_fd=destination)
    except FileNotFoundError:
        live = None
    try:
        for fname in kept:
            try:
                os.unlink(fname, dir_fd=staged_fd)
            except FileNotFoundError:
                pass
            if live is None:
                continue
            try:
                data, mode = _read(live, fname)
            except FileNotFoundError:
                continue
            _write(staged_fd, fname, data, mode)
    finally:
        if live is not None:
            os.close(live)


def _restore_tree(source, name, destination, target, staged):
    src = os.open(name, _DIR, dir_fd=source)
    old = staged + "-old"
    moved = False
    try:
        mode = _target_dir_mode(destination, target)
        os.mkdir(staged, mode, dir_fd=destination)
        dst = os.open(staged, _DIR, dir_fd=destination)
        try:
            _copy_tree(src, dst, 0, mode)
            _keep_live(destination, target, dst)
        finally:
            os.close(dst)
        try:
            os.rename(target, old, src_dir_fd=destination, dst_dir_fd=destination)
            moved = True
        except FileNotFoundError:
            pass
        try:
            os.rename(staged, target, src_dir_fd=destination, dst_dir_fd=destination)
        except BaseException:
            if moved:
                os.rename(old, target, src_dir_fd=destination, dst_dir_fd=destination)
            raise
        if moved:
            # A previous symlink is removed as an entry, never traversed.
            if stat.S_ISDIR(os.stat(old, dir_fd=destination, follow_symlinks=False).st_mode):
                shutil.rmtree(old, dir_fd=destination)
            else:
                os.unlink(old, dir_fd=destination)
    finally:
        os.close(src)
        try:
            shutil.rmtree(staged, dir_fd=destination)
        except FileNotFoundError:
            pass
