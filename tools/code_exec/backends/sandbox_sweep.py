"""Boot sweep over remote-sandbox records (098 CL-15).

A pack backend (``packs/daytona``, ``packs/modal``) writes a record the moment a
cloud sandbox exists: ``<data_home>/sandbox_state/<provider>/<slug>.json`` with
``{phase, sandbox_id, mode, owner}`` (``owner`` = ``ProcIdentity.encode()``).
A process that dies without ``teardown`` leaves that sandbox billed. Once per
process, at autonomy-runtime start (API lifespan, REPL, headless surfaces):

- the owner process is alive (same boot, pid, start time) -> leave it alone;
- no owner recorded (a legacy record) -> leave it alone (it may be live);
- ``mode == "ephemeral"`` and the owner is dead -> DELETE the sandbox, drop the record;
- ``mode == "persist"`` and the owner is dead -> STOP it (files survive), keep the
  record as ``phase="stopped"`` so the next setup for that session resumes it.

The provider work is the backend's ``reap_orphan(record) -> (outcome, record|None)``.
A provider whose pack is not loaded, or whose backend has no ``reap_orphan``, is
reported as skipped. Fail-open: every error is logged and counted, never raised.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

_DONE_PHASES = frozenset({"stopped", "released"})
_TASKS: Set[asyncio.Task] = set()


def _state_root(data_home: Optional[str]) -> Optional[Path]:
    if data_home:
        return Path(data_home) / "sandbox_state"
    try:
        from core.runtime_paths import effective_data_home
        return Path(effective_data_home()) / "sandbox_state"
    except Exception:
        return None


def _read(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, record: Optional[Dict[str, Any]]) -> None:
    if record is None:
        try:
            path.unlink()
        except OSError:
            pass
        return
    from core.state.files import atomic_write_json
    atomic_write_json(str(path), record, indent=None)


def _changed(path: Path, record: Dict[str, Any]) -> bool:
    """The record on disk no longer names the owner and sandbox the sweep read."""
    now = _read(path)
    return (now.get("owner"), now.get("sandbox_id")) != (record.get("owner"),
                                                         record.get("sandbox_id"))


def _owner_dead(record: Dict[str, Any]) -> Optional[bool]:
    """True = dead, False = alive, None = no owner recorded (unknown)."""
    from core.state.proc import ProcIdentity, is_alive
    owner = ProcIdentity.decode(record.get("owner")) if record.get("owner") else None
    if owner is None:
        return None
    return not is_alive(owner)


def _backend_for(provider: str):
    from tools.code_exec.pack_backends import backend_factory
    factory = backend_factory(provider)
    if factory is None:
        return None
    return factory(session_id=None, dev_mode=False)


async def sweep_orphan_sandboxes(*, data_home: Optional[str] = None,
                                 backend_for=None) -> Dict[str, List[str]]:
    """Run the sweep once; return ``{"deleted", "stopped", "gone", "skipped",
    "errors"}`` lists of ``provider:sandbox_id``."""
    report: Dict[str, List[str]] = {k: [] for k in
                                    ("deleted", "stopped", "gone", "skipped", "errors")}
    root = _state_root(data_home)
    if root is None or not root.is_dir():
        return report
    backend_for = backend_for or _backend_for
    for provider_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        provider = provider_dir.name
        backend = None
        for path in sorted(provider_dir.glob("*.json")):
            record = _read(path)
            sid = str(record.get("sandbox_id") or "")
            if not sid or record.get("phase") in _DONE_PHASES:
                continue
            label = f"{provider}:{sid}"
            try:
                if _owner_dead(record) is not True:
                    continue  # a live owner, or a legacy record with no owner
                if backend is None:
                    backend = backend_for(provider) or False
                reap = getattr(backend, "reap_orphan", None) if backend else None
                if not callable(reap):
                    report["skipped"].append(label)
                    continue
                if _changed(path, record):
                    report["skipped"].append(label)   # resumed by a live owner
                    continue
                outcome, new_record = await asyncio.to_thread(reap, dict(record))
                if _changed(path, record):
                    # 098 review (O-05): a session resumed while the reap ran —
                    # its new record is the truth; never write the old one back.
                    logger.warning("sandbox sweep: %s was resumed during the reap; "
                                   "record kept", label)
                    report["skipped"].append(label)
                    continue
                _write(path, new_record)
                report.setdefault(outcome, []).append(label)
            except Exception as exc:  # noqa: BLE001 — one record never stops the sweep
                logger.warning("sandbox sweep: %s failed: %s", label, exc)
                report["errors"].append(label)
    done = {k: v for k, v in report.items() if v}
    if done:
        logger.info("sandbox sweep (dead owners): %s",
                    "; ".join(f"{k} {', '.join(v)}" for k, v in done.items()))
    return report


def schedule_boot_sweep(data_home: Optional[str] = None) -> None:
    """Schedule the sweep on the running loop (once per process — the caller
    holds the cold-start latch). No record directory -> no task at all."""
    root = _state_root(data_home)
    if root is None or not root.is_dir():
        return

    async def _run() -> None:
        try:
            await sweep_orphan_sandboxes(data_home=data_home)
        except Exception as exc:
            logger.warning("sandbox sweep failed (non-fatal): %s", exc)

    task = asyncio.get_running_loop().create_task(_run())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


__all__ = ["sweep_orphan_sandboxes", "schedule_boot_sweep"]
