"""The pack seam for execution backends (073 W6/W7).

A pack contributes an execution backend WITHOUT a new core hook: its top-level
package (the module of its ``polyrob.packs`` entry point) defines

    def exec_backends() -> tuple[ExecBackendContribution, ...]

and this module PULLS it from every pack the loader marked LOADED
(``core.packs.state.loaded()``) — so the kill list, ``POLYROB_PACKS`` /
``POLYROB_PACKS_DISABLED`` and the custody rule of the 067 two-phase load all
apply before any backend code is imported. A disabled or refused pack
contributes nothing.

Each contribution registers:

- ``backend`` — a factory ``(*, session_id=None, dev_mode=False) -> ExecutionBackend``,
  registered by ``name`` in ``tools.code_exec.default_registry`` (so
  ``CODE_EXEC_BACKEND=<name>`` works for ``run_code``, and the sandbox guard reads
  its ``capabilities``). Constructing it must be cheap and import no SDK — the
  SDK is imported lazily in ``setup()`` with an honest error naming the extra.
- ``shell_executor`` — optional ``(*, session_id, workspace_dir, logs_dir) ->
  ShellExecutor`` (``tools/shell/executor_protocol.py``), so ``SHELL_BACKEND=<name>``
  works for the ``shell``/``process`` tools. ``tools/shell/remote_executor.py::
  backend_shell_executor`` builds one from a persistent backend.

Built-in names (``local_subprocess``, ``docker``, ``ssh``, ``host``, ``auto``) are
reserved: a pack never shadows a core backend.

No ``@BaseTool.action`` closures — ``from __future__`` is safe.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

RESERVED = frozenset({"local_subprocess", "docker", "ssh", "host", "auto"})
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


@dataclass(frozen=True)
class ExecBackendContribution:
    """One execution backend a pack contributes (see the module docstring)."""

    name: str
    backend: Any                       # "module:attr" or a callable factory
    shell_executor: Any = None         # "module:attr", a callable, or None
    summary: str = ""


@dataclass
class _Entry:
    contribution: ExecBackendContribution
    source: str


_ENTRIES: Dict[str, _Entry] = {}
_DISCOVERED: Set[str] = set()


class PackBackendError(ValueError):
    """A contribution is malformed or collides with another one."""


def _resolve(ref: Any) -> Any:
    from core.packs.spec import resolve_ref
    return resolve_ref(ref)


def register_exec_backend(contribution: ExecBackendContribution, *, source: str) -> None:
    """Register one contribution (idempotent for the same source)."""
    name = contribution.name
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise PackBackendError(f"backend name {name!r} must match {_NAME_RE.pattern}")
    if name in RESERVED:
        raise PackBackendError(f"backend name {name!r} is reserved for a core backend")
    prior = _ENTRIES.get(name)
    if prior is not None and prior.source != source:
        raise PackBackendError(f"backend {name!r} is already registered by {prior.source}")
    _ENTRIES[name] = _Entry(contribution, source)

    def _factory(*, session_id: Optional[str] = None, dev_mode: bool = False):
        return _resolve(contribution.backend)(session_id=session_id, dev_mode=dev_mode)

    from tools.code_exec.backend import default_registry
    default_registry.register(name, _factory)


def unregister_source(source: str) -> None:
    """Drop every backend a source registered (tests; a pack refused later)."""
    from tools.code_exec.backend import default_registry
    for name in [n for n, e in _ENTRIES.items() if e.source == source]:
        _ENTRIES.pop(name, None)
        default_registry.unregister(name)


def discover() -> None:
    """Pull ``exec_backends()`` from every LOADED pack not yet seen. Never raises:
    a pack's malformed contribution is logged and skipped (that backend is then
    simply unknown, and an explicit choice of it refuses by name)."""
    try:
        from core.packs import state
        loaded = list(state.loaded())
    except Exception:
        logger.debug("pack backends: pack state unreadable", exc_info=True)
        return
    for rec in loaded:
        if rec.id in _DISCOVERED:
            continue
        _DISCOVERED.add(rec.id)
        try:
            import importlib
            module = importlib.import_module(str(rec.entry_point).partition(":")[0])
            fn = getattr(module, "exec_backends", None)
            if not callable(fn):
                continue
            for contribution in fn() or ():
                if not isinstance(contribution, ExecBackendContribution):
                    raise PackBackendError(
                        f"exec_backends() returned {type(contribution).__name__}, "
                        "not an ExecBackendContribution")
                register_exec_backend(contribution, source=f"pack:{rec.id}")
        except Exception as exc:  # noqa: BLE001 — one pack never stops another
            logger.error("pack %r: execution backend contribution refused: %s", rec.id, exc)


def backend_names() -> List[str]:
    discover()
    return sorted(_ENTRIES)


def shell_backend_names() -> List[str]:
    """Names usable as ``SHELL_BACKEND`` (a contribution with a shell executor)."""
    discover()
    return sorted(n for n, e in _ENTRIES.items() if e.contribution.shell_executor is not None)


def is_pack_backend(name: str) -> bool:
    discover()
    return name in _ENTRIES


def backend_factory(name: str) -> Optional[Callable[..., Any]]:
    """``(*, session_id=None, dev_mode=False) -> ExecutionBackend`` or None."""
    discover()
    entry = _ENTRIES.get(name)
    if entry is None:
        return None
    return lambda **kw: _resolve(entry.contribution.backend)(**kw)


def shell_executor_factory(name: str) -> Optional[Callable[..., Any]]:
    """``(*, session_id, workspace_dir, logs_dir) -> ShellExecutor`` or None."""
    discover()
    entry = _ENTRIES.get(name)
    if entry is None or entry.contribution.shell_executor is None:
        return None
    return lambda **kw: _resolve(entry.contribution.shell_executor)(**kw)


def source_of(name: str) -> Optional[str]:
    entry = _ENTRIES.get(name)
    return entry.source if entry else None


def reset_for_tests() -> None:
    for source in {e.source for e in _ENTRIES.values()}:
        unregister_source(source)
    _DISCOVERED.clear()


__all__ = ["ExecBackendContribution", "PackBackendError", "RESERVED", "backend_factory",
           "backend_names", "discover", "is_pack_backend", "register_exec_backend",
           "reset_for_tests", "shell_backend_names", "shell_executor_factory", "source_of",
           "unregister_source"]
