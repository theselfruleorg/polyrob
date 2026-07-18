"""Durable telemetry event log (telemetry audit 2026-07-04, Phase 2 foundation).

A small append-only SQLite sink for the signals the structured `*TelemetryEvent`
feed pipeline never covered: autonomy-loop lifecycle (cron/goal/self-wake/curator/
background-review) and the governance surface (tool denials, timeouts, rate-limit
rejections, wallet spend). Those live today in bare `logger.*` breadcrumbs or
in-memory lists that vanish on restart, with no cross-session/operator view.

Design goals: tenant-scoped, cheap, and FAIL-OPEN — telemetry must never break the
thing it observes. Uses the shared WAL+jitter helper (core/sqlite_util) so it is
safe under workers>1, mirroring goals.db / cron.db.

This is the durable layer; a fleet/query API and the individual emitters build on
top of it in later increments.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from core.sqlite_util import execute_retry, wal_connect

logger = logging.getLogger("task.telemetry.event_log")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    kind       TEXT NOT NULL,
    user_id    TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',
    attrs      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_te_ts   ON telemetry_events(ts);
CREATE INDEX IF NOT EXISTS idx_te_kind ON telemetry_events(kind);
CREATE INDEX IF NOT EXISTS idx_te_user ON telemetry_events(user_id);
"""


class TelemetryEventLog:
    """Append-only durable event sink. Every method is fail-open."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ready = False
        try:
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
            conn = wal_connect(db_path)
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()
            self._ready = True
        except Exception as e:
            # Fail-open: a broken telemetry DB must never break the agent.
            logger.debug(f"event_log init failed ({db_path}): {e}")

    def record(self, kind: str, *, user_id: str = "", session_id: str = "",
               source: str = "", ts: Optional[float] = None,
               attrs: Optional[Dict[str, Any]] = None, **kw: Any) -> None:
        """Append one event. Extra kwargs are JSON-encoded into `attrs`.

        ``attrs`` also accepts an explicit dict — the escape hatch for attribute
        names that collide with this signature (e.g. a `kind` attribute on a
        self_modification event, T4-06). Explicit-dict keys win over kwargs.
        """
        if not self._ready:
            return
        merged = dict(kw)
        if attrs:
            try:
                merged.update(attrs)
            except Exception:
                pass
        try:
            payload = json.dumps(merged, default=str)
        except Exception:
            payload = "{}"
        try:
            execute_retry(
                self.db_path,
                "INSERT INTO telemetry_events (ts, kind, user_id, session_id, source, attrs) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (float(ts if ts is not None else time.time()), str(kind),
                 str(user_id or ""), str(session_id or ""), str(source or ""), payload),
            )
        except Exception as e:
            logger.debug(f"event_log record failed: {e}")

    def query(self, *, since_ts: Optional[float] = None, kind: Optional[str] = None,
              user_id: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        """Return events most-recent-first, with optional filters."""
        if not self._ready:
            return []
        clauses, params = [], []
        if since_ts is not None:
            clauses.append("ts >= ?"); params.append(float(since_ts))
        if kind is not None:
            clauses.append("kind = ?"); params.append(str(kind))
        if user_id is not None:
            clauses.append("user_id = ?"); params.append(str(user_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (f"SELECT ts, kind, user_id, session_id, source, attrs "
               f"FROM telemetry_events{where} ORDER BY ts DESC, id DESC LIMIT ?")
        params.append(int(limit))
        try:
            rows = execute_retry(self.db_path, sql, tuple(params), fetch="all") or []
        except Exception as e:
            logger.debug(f"event_log query failed: {e}")
            return []
        out = []
        for r in rows:
            try:
                attrs = json.loads(r["attrs"]) if r["attrs"] else {}
            except Exception:
                attrs = {}
            out.append({"ts": r["ts"], "kind": r["kind"], "user_id": r["user_id"],
                        "session_id": r["session_id"], "source": r["source"], "attrs": attrs})
        return out

    def prune(self, *, older_than_ts: float) -> int:
        """Delete events older than a cutoff. Returns rows removed (keeps the store
        bounded — the same retention discipline the audit demanded of feed/)."""
        if not self._ready:
            return 0
        try:
            return int(execute_retry(
                self.db_path,
                "DELETE FROM telemetry_events WHERE ts < ?",
                (float(older_than_ts),),
            ) or 0)
        except Exception as e:
            logger.debug(f"event_log prune failed: {e}")
            return 0

    def aggregate(self, *, since_ts: Optional[float] = None,
                  user_id: Optional[str] = None) -> Dict[str, Any]:
        """Cross-session rollup: counts per kind + total wallet spend."""
        from core.event_kinds import WALLET_SPEND
        rows = self.query(since_ts=since_ts, user_id=user_id, limit=100000)
        counts: Dict[str, int] = {}
        total_spend = 0.0
        for r in rows:
            counts[r["kind"]] = counts.get(r["kind"], 0) + 1
            if r["kind"] == WALLET_SPEND:
                try:
                    total_spend += float(r["attrs"].get("amount_usd") or 0.0)
                except Exception:
                    pass
        return {"counts_by_kind": counts, "wallet_spend_usd": total_spend,
                "total_events": len(rows)}


# --- process-wide singleton keyed by db path -------------------------------------
_INSTANCES: Dict[str, TelemetryEventLog] = {}


def get_event_log(db_path: Optional[str] = None) -> TelemetryEventLog:
    """Get/create the shared event log. Default path lives under the DATA HOME
    (R-2 T1: ``core.runtime_paths.sidecar_db_path`` — the db_manifest axis, with a
    read-both fallback to a pre-existing session-tree file).

    ``TELEMETRY_EVENT_LOG_PATH`` overrides the default resolution — the seam the
    test suite uses to keep durable telemetry (and the §3.2 delivery-rail
    memory) out of the developer's real data home.
    """
    if db_path is None:
        db_path = os.getenv("TELEMETRY_EVENT_LOG_PATH") or None
    _relocated: list = []
    if db_path is None:
        # R-2 T3: first default resolution in the process runs the one-shot
        # legacy->data-home sweep BEFORE the singleton binds, so the instance
        # starts on the new path and never forks history. Fail-open + idempotent.
        try:
            from core.sidecar_relocate import relocate_legacy_sidecars
            _relocated = relocate_legacy_sidecars()
        except Exception:
            pass
        from core.runtime_paths import sidecar_db_path
        db_path = str(sidecar_db_path("telemetry_events.db"))
    inst = _INSTANCES.get(db_path)
    if inst is None:
        inst = TelemetryEventLog(db_path)
        if _relocated:
            try:
                from core.event_kinds import DB_RELOCATED
                inst.record(DB_RELOCATED, attrs={"names": _relocated})
            except Exception:
                pass
        _INSTANCES[db_path] = inst
    return inst


def event_log_enabled() -> bool:
    """Additive observability sink; default ON, fail-open. Disable with =off/false/0."""
    v = os.getenv("TELEMETRY_EVENT_LOG_ENABLED", "true").strip().lower()
    return v not in ("0", "false", "off", "no", "")
