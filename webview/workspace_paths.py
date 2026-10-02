"""The ONE containment rule for a workspace-relative path (WS7).

Both workspace file routes (``/workspace/file`` and ``/workspace/serve/``)
used to carry a SUBSTRING deny-list (``windows``, ``c:``, ``/home/``, ``/.``)
that refused real files — ``docs/windows-setup.md``, ``site/.well-known/`` —
and added nothing the containment check below does not already enforce.

The rule: refuse a ``..`` component, an absolute path (``/``, ``\\``, a drive
letter) or a NUL — in the path as given AND once more URL-decoded (a
double-encoded ``%2e%2e`` is refused, not served) — then resolve it (symlinks
included) and require it to stay under the resolved workspace directory.
"""
import logging
import re
from pathlib import Path
from urllib.parse import unquote

logger = logging.getLogger(__name__)

_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_SEP = re.compile(r"[\\/]")


def _escapes(candidate: str) -> bool:
    return (candidate.startswith(("/", "\\")) or bool(_DRIVE.match(candidate))
            or "\x00" in candidate or ".." in _SEP.split(candidate))


class OutsideWorkspace(ValueError):
    """The path escapes the workspace (the route answers 403)."""


def resolve_in_workspace(workspace_dir: Path, raw: str) -> Path:
    """The resolved file path for ``raw`` inside ``workspace_dir``.

    Raises :class:`OutsideWorkspace` when the path escapes the workspace, and
    lets an ``OSError`` from resolving through. It does not check that the file
    exists — the caller answers that. The words a person reads stay in the
    route (``server._workspace_target``).
    """
    if _escapes(raw) or _escapes(unquote(raw)):
        logger.warning("Rejected workspace path outside the workspace: %r", raw)
        raise OutsideWorkspace(raw)
    root = Path(workspace_dir).resolve()
    target = (root / raw).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        logger.warning("Workspace path resolves outside the workspace: %r", raw)
        raise OutsideWorkspace(raw) from None
    return target
