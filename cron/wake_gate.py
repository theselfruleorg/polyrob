"""Wake change-gate: skip a change-gated cron
review tick when nothing observable changed since the last tick.

The observed autonomy economy was ~23/25 no-op review wakes — every tick paid
for a full model run to conclude "nothing new". The gate computes a cheap
fingerprint over the tenant's observable autonomy state (goal board + goal
events + cron activity + newest episode + x402 payment-request state) and
compares it to the baseline stored
at the previous tick; equality means the paid model call is skipped ($0 tick,
same shape as ``wake_agent=False``).

Scope guards (both must hold before a tick can be skipped):
- global: ``AutonomyConfig.wake_change_gate()`` (``WAKE_CHANGE_GATE``, posture
  ``full`` turns it on by default — it pairs with ``CRON_ENABLED``);
- per-job: ``payload.change_gated`` truthy AND no ``payload.deliver`` — a
  scheduled delivery (daily digest) must fire even with zero change.

Fail-open everywhere: a missing DB, a schema surprise, or any exception in a
fingerprint source contributes a neutral value or disables the skip — the gate
can only ever suppress a tick when every read succeeded AND matched the
baseline. The baseline is recorded AFTER a run, tagged with the run's outcome
(``record_wake_outcome``): a skip requires the last gated run to have SUCCEEDED,
so a persistently-failing job (which mutates no observable state and would
otherwise fingerprint as "unchanged") always retries instead of being silently
swallowed.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import time
from typing import Any, Callable, Optional

from core.sqlite_util import execute_retry

logger = logging.getLogger(__name__)


def _neutral_on_missing_table(fn: Callable[[], str], neutral: str) -> str:
    """A missing table/DB is a known-empty source (nothing to observe) → a stable
    neutral token. Any OTHER error re-raises so the caller's fail-open varying
    token forces the tick to run."""
    try:
        return fn()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return neutral
        raise


class WakeGateStore:
    """Per-job fingerprint baselines, colocated in cron.db (the gate is a cron
    concern; a side table keeps the baseline server-owned and out of the
    agent-mutable job payload)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        execute_retry(
            db_path,
            """CREATE TABLE IF NOT EXISTS wake_gate (
                   job_id TEXT PRIMARY KEY,
                   user_id TEXT,
                   fingerprint TEXT NOT NULL,
                   ok INTEGER NOT NULL DEFAULT 1,
                   updated_at REAL NOT NULL
               )""",
        )
        try:  # idempotent migration for pre-`ok` tables
            execute_retry(db_path, "ALTER TABLE wake_gate ADD COLUMN ok INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            pass

    def get(self, job_id: str) -> Optional[dict]:
        row = execute_retry(
            self.db_path, "SELECT fingerprint, ok FROM wake_gate WHERE job_id=?",
            (job_id,), fetch="one",
        )
        return dict(row) if row else None

    def put(self, job_id: str, user_id: str, fingerprint: str, *, ok: bool = True) -> None:
        execute_retry(
            self.db_path,
            """INSERT INTO wake_gate (job_id, user_id, fingerprint, ok, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET
                   fingerprint=excluded.fingerprint, ok=excluded.ok,
                   updated_at=excluded.updated_at""",
            (job_id, user_id, fingerprint, 1 if ok else 0, time.time()),
        )


def _goal_board_cursor(user_id: str, data_dir: str) -> str:
    """MAX(goal_events.id) for the tenant + goal counts by status — bumps on every
    goal/ask/objective mutation (goal_events is AUTOINCREMENT)."""
    db = os.path.join(data_dir, "goals.db")
    if not os.path.exists(db):
        return "goals:none"
    return _neutral_on_missing_table(lambda: _goal_board_read(user_id, db), "goals:none")


def _goal_board_read(user_id: str, db: str) -> str:
    max_ev = execute_retry(
        db,
        "SELECT MAX(e.id) AS m FROM goal_events e JOIN goals g ON g.id=e.goal_id "
        "WHERE g.user_id=?",
        (user_id,), fetch="one",
    )
    counts = execute_retry(
        db,
        "SELECT status, COUNT(*) AS n FROM goals WHERE user_id=? GROUP BY status "
        "ORDER BY status",
        (user_id,), fetch="all",
    )
    count_sig = ",".join(f"{r['status']}={r['n']}" for r in (counts or []))
    return f"goals:{(max_ev or {})['m'] if max_ev else None}:{count_sig}"


def _cron_cursor(user_id: str, data_dir: str, exclude_job_id: Optional[str]) -> str:
    """Newest last_run_at across the tenant's OTHER cron jobs (another job having
    run is observable change for a review tick)."""
    db = os.path.join(data_dir, "cron.db")
    if not os.path.exists(db):
        return "cron:none"

    def read() -> str:
        row = execute_retry(
            db,
            "SELECT MAX(last_run_at) AS m FROM cron_jobs WHERE user_id=? AND id != ?",
            (user_id, exclude_job_id or ""), fetch="one",
        )
        return f"cron:{row['m'] if row else None}"

    return _neutral_on_missing_table(read, "cron:none")


def _episode_cursor(user_id: str, data_dir: str) -> str:
    """Newest episode row for the tenant (a completed session/goal/cron run since
    the last tick is observable change). Reads the memory DB directly and
    fail-opens to neutral on any surprise — the provider owns the schema."""
    for name in ("memory.db",):
        db = os.path.join(data_dir, name)
        if not os.path.exists(db):
            continue
        def read() -> str:
            row = execute_retry(
                db, "SELECT MAX(id) AS m FROM episodes WHERE user_id=?",
                (user_id,), fetch="one",
            )
            return f"episodes:{row['m'] if row else None}"

        return _neutral_on_missing_table(read, "episodes:none")
    return "episodes:none"


# Task 12 (Phase 2, G-36): mirrors modules.x402.invoicing.INVOICE_KIND. Not
# imported from there directly — every leg here reads its owning module's
# table with raw SQL rather than importing the module, so this fingerprint
# stays cheap and dependency-light (no fastapi_x402/web3 import chain on
# every cron tick just to read a literal).
_AGENT_INVOICE_LIKE = '%"kind": "agent_invoice"%'


def _x402_cursor(user_id: str, data_dir: str) -> str:
    """Tenant-scoped x402 payment-request signal (G-36): newest update time +
    per-status row counts for this tenant's agent invoices
    (``x402_payment_requests`` in ``data_dir/database/bot.db`` — see
    ``modules/database/database_manager.py``). A settlement
    (pending->completed), an expiry (pending->expired), or a new invoice
    changes this leg, so a change-gated job watching payment events is never
    skipped on the very tick it should react to.

    Tenant match mirrors the SAFE pattern established across modules/x402 and
    modules/credits/unified_ledger.py: the ``user_id`` column OR
    ``json_extract(metadata, '$.tenant_id')`` — NOT a ``metadata LIKE`` on the
    tenant id, which would misfire (SQLite LIKE treats ``_`` as a wildcard,
    and real tenant ids look like ``u_<hex>``)."""
    db = os.path.join(data_dir, "database", "bot.db")
    if not os.path.exists(db):
        return "x402:none"
    return _neutral_on_missing_table(lambda: _x402_read(user_id, db), "x402:none")


def _x402_read(user_id: str, db: str) -> str:
    newest = execute_retry(
        db,
        "SELECT MAX(updated_at) AS m FROM x402_payment_requests "
        "WHERE (user_id=? OR json_extract(metadata,'$.tenant_id')=?) AND metadata LIKE ?",
        (user_id, user_id, _AGENT_INVOICE_LIKE), fetch="one",
    )
    counts = execute_retry(
        db,
        "SELECT status, COUNT(*) AS n FROM x402_payment_requests "
        "WHERE (user_id=? OR json_extract(metadata,'$.tenant_id')=?) AND metadata LIKE ? "
        "GROUP BY status ORDER BY status",
        (user_id, user_id, _AGENT_INVOICE_LIKE), fetch="all",
    )
    count_sig = ",".join(f"{r['status']}={r['n']}" for r in (counts or []))
    return f"x402:{(newest or {})['m'] if newest else None}:{count_sig}"


def compute_wake_fingerprint(
    user_id: str, *, data_dir: str, exclude_job_id: Optional[str] = None
) -> str:
    """Stable hash of the tenant's observable autonomy state (cheap, no LLM)."""
    parts = []
    for source in (
        lambda: _goal_board_cursor(user_id, data_dir),
        lambda: _cron_cursor(user_id, data_dir, exclude_job_id),
        lambda: _episode_cursor(user_id, data_dir),
        lambda: _x402_cursor(user_id, data_dir),
    ):
        try:
            parts.append(source())
        except Exception:
            # a failing source contributes a per-call-varying token so the
            # fingerprint reads as "changed" — fail-open, never suppress a wake
            parts.append(f"err:{time.time()}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def gate_applies(job: Any) -> bool:
    """Both guards: the global flag AND the per-job opt-in (never delivery jobs)."""
    try:
        from core.config_policy import AutonomyConfig
        if not AutonomyConfig.wake_change_gate():
            return False
        payload = dict(getattr(job, "payload", None) or {})
        return bool(payload.get("change_gated")) and not payload.get("deliver")
    except Exception:
        return False


def should_skip_wake(job: Any, *, data_dir: str) -> bool:
    """Whether this change-gated tick can be skipped ($0). READ-ONLY: a skip
    requires an existing baseline that (a) matches the current fingerprint and
    (b) was recorded after a SUCCESSFUL run — a failed run never establishes a
    skippable baseline, so failing jobs always retry. Callers record the
    baseline after the run via :func:`record_wake_outcome`."""
    try:
        if not gate_applies(job):
            return False
        store = WakeGateStore(os.path.join(data_dir, "cron.db"))
        baseline = store.get(job.id)
        if not baseline or not baseline.get("ok"):
            return False
        fp = compute_wake_fingerprint(
            job.user_id, data_dir=data_dir, exclude_job_id=getattr(job, "id", None)
        )
        return fp == baseline.get("fingerprint")
    except Exception:
        logger.warning("wake gate error — failing open (running the tick)", exc_info=True)
        return False


def record_wake_outcome(job: Any, *, data_dir: str, ok: bool) -> None:
    """Record the post-run baseline (fingerprint computed AFTER the run, so the
    run's own writes don't read as 'change' next tick) tagged with the outcome.
    Fail-open — a store error just means the next tick runs."""
    try:
        if not gate_applies(job):
            return
        store = WakeGateStore(os.path.join(data_dir, "cron.db"))
        fp = compute_wake_fingerprint(
            job.user_id, data_dir=data_dir, exclude_job_id=getattr(job, "id", None)
        )
        store.put(job.id, job.user_id, fp, ok=ok)
    except Exception:
        logger.warning("wake gate outcome record failed (next tick will run)", exc_info=True)
