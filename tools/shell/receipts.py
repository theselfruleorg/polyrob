"""Durable receipts for finished background shell jobs (073 W4, cross-agent parity).

The in-memory ``ProcessRegistry`` is the LIVE view and dies with the process. When
a job finishes, one JSON receipt goes to the session ``data/shell_jobs/`` dir
(``pm().get_data_dir``): id, command (secret shapes redacted), backend, started,
finished, status, exit code, and a redacted log tail of at most 200 000 chars
(``tools/shell/output.shape_output``). ``process`` list/poll/log/wait fall back to
a receipt for a job the registry lost (a restart).

Retention: 7 days and 64 receipts per session, pruned on every write. Files are
0600 in a 0700 dir. Every function fails open (a receipt is a record, never a
reason for a shell call to fail).

No ``@BaseTool.action`` closures — ``from __future__`` is safe.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

RECEIPT_MAX_AGE_SEC = 7 * 86400
RECEIPT_MAX_PER_SESSION = 64
RECEIPT_LOG_MAX_CHARS = 200_000


def receipts_dir(session_id: str, user_id: Optional[str]) -> Optional[Path]:
    """``<session data dir>/shell_jobs`` or None when the path cannot resolve."""
    try:
        from agents.task.path import pm
        return Path(pm().get_data_dir(session_id or "shell", user_id)) / "shell_jobs"
    except Exception:
        logger.debug("shell receipts: data dir unresolved", exc_info=True)
        return None


def _path(d: Path, job_id: str) -> Path:
    from tools.shell.executor import _safe_job_id
    return d / f"{_safe_job_id(job_id)}.json"


def prune(d: Path, *, now: Optional[float] = None) -> None:
    """Drop receipts older than 7 days, then keep the newest 64."""
    now = time.time() if now is None else now
    try:
        files = [p for p in d.glob("*.json") if p.is_file()]
    except OSError:
        return
    keep = []
    for p in files:
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        if age > RECEIPT_MAX_AGE_SEC:
            try:
                p.unlink()
            except OSError:
                pass
        else:
            keep.append(p)
    keep.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)
    for p in keep[RECEIPT_MAX_PER_SESSION:]:
        try:
            p.unlink()
        except OSError:
            pass


def write_receipt(session_id: str, user_id: Optional[str], job, *, status: str,
                  exit_code: Optional[int], log_text: str,
                  now: Optional[float] = None, overwrite: bool = False,
                  extra: Optional[Dict[str, Any]] = None) -> Optional[Path]:
    """Write the receipt for ``job`` (a ``process_registry.Job``). Idempotent
    unless ``overwrite`` (a kill corrects the status the watcher recorded)."""
    d = receipts_dir(session_id, user_id)
    if d is None:
        return None
    now = time.time() if now is None else now
    try:
        path = _path(d, job.id)
        if path.exists() and not overwrite:
            return path
        from core.secret_scrub import scrub_secret_shapes
        from tools.shell.output import shape_output
        tail, _ = shape_output(log_text or "", max_chars=RECEIPT_LOG_MAX_CHARS)
        record = {
            "id": job.id,
            "session_id": session_id,
            "command": scrub_secret_shapes(job.command or ""),
            "backend": getattr(job, "backend", "docker"),
            "started": job.created_at,
            "finished": getattr(job, "finished_at", None) or now,
            "status": status,
            "exit_code": exit_code,
            "log_tail": tail,
        }
        if extra:
            record.update(extra)
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        tmp = path.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        os.replace(tmp, path)
        prune(d, now=now)
        return path
    except Exception:
        logger.debug("shell receipt write failed (fail-open)", exc_info=True)
        return None


def load_receipt(session_id: str, user_id: Optional[str], job_id: str) -> Optional[Dict[str, Any]]:
    d = receipts_dir(session_id, user_id)
    if d is None:
        return None
    try:
        path = _path(d, job_id)
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > RECEIPT_MAX_AGE_SEC:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_receipts(session_id: str, user_id: Optional[str]) -> List[Dict[str, Any]]:
    """This session's receipts, oldest first (expired ones skipped)."""
    d = receipts_dir(session_id, user_id)
    if d is None or not d.is_dir():
        return []
    out = []
    now = time.time()
    try:
        for p in d.glob("*.json"):
            try:
                if now - p.stat().st_mtime > RECEIPT_MAX_AGE_SEC:
                    continue
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                continue
    except OSError:
        return []
    out.sort(key=lambda r: r.get("started") or 0.0)
    return out


def receipt_status_line(rec: Dict[str, Any]) -> str:
    from tools.shell.output import exit_note
    rc = rec.get("exit_code")
    status = rec.get("status") or "done"
    if rc is None:
        return f"job `{rec.get('id')}`: {status} (from the durable receipt)"
    note = exit_note(rc)
    return (f"job `{rec.get('id')}`: {status} (exit {rc}{' — ' + note if note else ''}; "
            f"from the durable receipt)")


async def finalize_job(executor, job, *, session_id: str, user_id: Optional[str],
                       registry=None, status: Optional[str] = None,
                       overwrite: bool = False) -> Optional[Path]:
    """Read the finished job's exit code + log through its executor and write the
    receipt. ``status`` overrides the executor's view (``killed``)."""
    import asyncio
    try:
        rc = None
        for _ in range(10):  # a pty pump writes .rc a moment after the exit
            rc = await executor.exit_code(job.id)
            if rc is not None or status == "killed":
                break
            await asyncio.sleep(0.1)
        log = await executor.read_log(job.id, max_bytes=RECEIPT_LOG_MAX_CHARS)
    except Exception:
        logger.debug("shell receipt: executor read failed", exc_info=True)
        rc, log = None, ""
    final = status or (job.status if job.status in ("killed",) else "done")
    if registry is not None:
        try:
            registry.mark(session_id, job.id, final, now=time.time())
        except Exception:
            pass
    return write_receipt(session_id, user_id, job, status=final, exit_code=rc,
                         log_text=log, overwrite=overwrite)


__all__ = [
    "receipts_dir", "write_receipt", "load_receipt", "list_receipts", "prune",
    "finalize_job", "receipt_status_line", "RECEIPT_MAX_AGE_SEC",
    "RECEIPT_MAX_PER_SESSION", "RECEIPT_LOG_MAX_CHARS",
]
