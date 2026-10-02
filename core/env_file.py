"""Env-file read/upsert primitives (018 P1).

Promoted from ``cli/commands/config.py::_upsert_env``/``_read_env_file`` so the
core config service can write env flags without importing upward into the cli
tier (the cli module now delegates here). Semantics preserved exactly:
whitespace-normalized key matching (a hand-edited ``KEY = value`` line is
updated in place, never duplicated), 0600 on secure writes, comment lines kept.
"""
import os
import re
import tempfile
from pathlib import Path
from typing import Optional, Union

#: An env key the writer accepts: upper-case, digits, underscore (M12).
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
#: Characters that would end the ``KEY=value`` line early and let a value
#: smuggle a SECOND line — a flag the caller was never allowed to write (M12).
_LINE_BREAKERS = ("\r", "\n", "\0")


def env_write_error(key: str, value: str) -> str:
    """Why ``KEY=value`` must not be written, or ``""`` when it is safe.

    Security review 2026-09-23 (M12): a console value carrying ``\n`` wrote an
    extra line into ``.env`` — an unwritable flag (``POLYROB_LOCAL``, the owner
    id, the exec backend) set through a writable one. One check, at the one
    writer, so every caller inherits it.
    """
    k = (key or "").strip()
    if not ENV_KEY_RE.fullmatch(k):
        return f"invalid env key {key!r}: expected [A-Z][A-Z0-9_]*"
    v = "" if value is None else str(value)
    if any(c in v for c in _LINE_BREAKERS):
        return f"invalid value for {k}: a value must not contain CR, LF or NUL"
    return ""


def _write_target(path: Path) -> Path:
    """The file an atomic write replaces. A symlink is followed only when THIS
    user owns the link (a dotfile manager's ``.env -> ~/dotfiles/.env``); a link
    planted by someone else is refused rather than written through."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return path
    if os.path.islink(path):
        if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
            raise PermissionError(f"refusing to write through a symlink owned by uid {st.st_uid}: {path}")
        return Path(os.path.realpath(path))
    return path


def write_private_file(path: Union[str, Path], data: Union[str, bytes], *,
                       mode: Optional[int] = 0o600) -> None:
    """Write *data* to *path* atomically with no permissive window.

    The bytes go to a temp file in the SAME directory created
    ``O_CREAT|O_EXCL`` at mode 0600 (``tempfile.mkstemp``), are fsynced, get
    their final *mode* (``None`` = keep the existing file's mode, else 0644),
    and are ``os.replace``d over the target — never ``write_text`` then
    ``chmod``, which left a secret world-readable for the gap and truncated the
    file in place on a crash.
    """
    target = _write_target(Path(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        old = os.stat(target)
    except FileNotFoundError:
        old = None
    if mode is None:
        mode = (old.st_mode & 0o7777) if old is not None else 0o644
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
            if old is not None and hasattr(os, "fchown"):
                # Keep the replaced file's owner/group (root editing a service's
                # env file must not leave it unreadable to that service).
                try:
                    os.fchown(fd, old.st_uid, old.st_gid)
                except OSError:
                    pass
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        os.replace(tmp, str(target))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def upsert_env_var(path: Path, key: str, value: str, *, secure: bool = True) -> None:
    """Insert or update ``KEY=value`` in *path*, preserving other lines.

    Raises ``ValueError`` for a malformed key or a value carrying CR/LF/NUL
    (see :func:`env_write_error`) — before the file is touched.
    """
    err = env_write_error(key, value)
    if err:
        raise ValueError(err)
    lines = path.read_text().splitlines() if path.exists() else []
    index = {ln.split("=", 1)[0].strip(): i for i, ln in enumerate(lines) if "=" in ln}
    key = key.strip()
    line = f"{key}={value}"
    if key in index:
        lines[index[key]] = line
    else:
        lines.append(line)
    write_private_file(path, "\n".join(lines) + "\n", mode=0o600 if secure else None)


def remove_env_var(path: Path, key: str, *, secure: bool = True) -> bool:
    """Remove ``KEY=...`` from *path*, preserving other lines.

    Same whitespace-normalized key matching as :func:`upsert_env_var` (a
    hand-edited ``KEY = value`` line is found too). Returns True when a line
    was removed, False when the key (or the file) was absent.
    """
    if not path.exists():
        return False
    lines = path.read_text().splitlines()
    key = key.strip()
    kept = [ln for ln in lines
            if not ("=" in ln and ln.split("=", 1)[0].strip() == key)]
    if len(kept) == len(lines):
        return False
    write_private_file(path, "\n".join(kept) + ("\n" if kept else ""),
                       mode=0o600 if secure else None)
    return True


def read_env_file(path: Path) -> dict:
    """Parse ``KEY=value`` lines (comments/blank skipped); {} when absent."""
    result: dict = {}
    if path.exists():
        for ln in path.read_text().splitlines():
            stripped = ln.strip()
            if "=" in stripped and not stripped.startswith("#"):
                k, v = stripped.split("=", 1)
                result[k.strip()] = v.strip()
    return result
