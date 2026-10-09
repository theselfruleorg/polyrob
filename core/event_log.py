"""Durable telemetry event log (telemetry audit 2026-07-04, Phase 2 foundation).

Core-tier home since S3 (2026-08-29): relocated from agents/task/telemetry/event_log.py —
it imported only core, and eight core modules plus two modules/ files imported it upward.

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
import math
import os
import time
from typing import Any, Dict, List, Optional

from core.sqlite_util import execute_retry, init_schema, wal_connect

logger = logging.getLogger("task.telemetry.event_log")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    kind       TEXT NOT NULL,
    user_id    TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',
    attrs      TEXT NOT NULL DEFAULT '{}',
    effect     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_te_ts   ON telemetry_events(ts);
CREATE INDEX IF NOT EXISTS idx_te_kind ON telemetry_events(kind);
CREATE INDEX IF NOT EXISTS idx_te_user ON telemetry_events(user_id);
"""


def _migrate_effect_column(db_path: str) -> None:
    """033: the effect class of an ``external_write`` row, promoted to an indexed
    column because ``attrs`` is opaque JSON a reader cannot filter cheaply.
    ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so an in-place
    upgrade adds the column here. Old rows read ``effect=''``."""
    conn = wal_connect(db_path)
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(telemetry_events)").fetchall()}
        if "effect" not in cols:
            conn.execute("ALTER TABLE telemetry_events "
                         "ADD COLUMN effect TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_te_effect "
                     "ON telemetry_events(effect)")
        conn.commit()
    finally:
        conn.close()


class TelemetryEventLog:
    """Append-only sink. Writes fail open; financial aggregation raises on bad reads."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ready = False
        #: False only on a pre-033 file the migration could not alter (read-only
        #: mount); reads then synthesize ``effect=''`` instead of failing.
        self._has_effect = True
        try:
            init_schema(db_path, _SCHEMA, mkdir=True)
            self._ready = True
        except Exception as e:
            # Fail-open: a broken telemetry DB must never break the agent.
            logger.debug(f"event_log init failed ({db_path}): {e}")
            return
        try:
            _migrate_effect_column(db_path)
        except Exception as e:
            # A read-only or contended file keeps working without the column;
            # an effect-filtered read then fails open to "no rows".
            logger.debug(f"event_log effect column migration skipped ({db_path}): {e}")
            self._has_effect = False

    def record(self, kind: str, *, user_id: str = "", session_id: str = "",
               source: str = "", ts: Optional[float] = None,
               attrs: Optional[Dict[str, Any]] = None, effect: str = "",
               **kw: Any) -> None:
        """Append one event. Extra kwargs are JSON-encoded into `attrs`.

        ``attrs`` also accepts an explicit dict — the escape hatch for attribute
        names that collide with this signature (e.g. a `kind` attribute on a
        self_modification event, T4-06). Explicit-dict keys win over kwargs.

        ``effect`` (033) is written to its own indexed column, never into
        ``attrs``; only ``external_write`` rows carry one.
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
            row = (float(ts if ts is not None else time.time()), str(kind),
                   str(user_id or ""), str(session_id or ""), str(source or ""), payload)
            if effect and self._has_effect:
                execute_retry(
                    self.db_path,
                    "INSERT INTO telemetry_events (ts, kind, user_id, session_id, source, "
                    "attrs, effect) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    row + (str(effect),),
                )
            else:
                execute_retry(
                    self.db_path,
                    "INSERT INTO telemetry_events (ts, kind, user_id, session_id, source, attrs) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    row,
                )
        except Exception as e:
            logger.debug(f"event_log record failed: {e}")

    def query(self, *, since_ts: Optional[float] = None, kind: Optional[str] = None,
              user_id: Optional[str] = None, limit: int = 500,
              effect: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return events most-recent-first, with optional filters.

        Every row carries ``effect`` (``''`` for a row that is not an
        ``external_write``, or one written before the column existed).
        """
        if not self._ready:
            return []
        clauses, params = [], []
        if since_ts is not None:
            clauses.append("ts >= ?"); params.append(float(since_ts))
        if kind is not None:
            clauses.append("kind = ?"); params.append(str(kind))
        if user_id is not None:
            clauses.append("user_id = ?"); params.append(str(user_id))
        if effect is not None:
            if not self._has_effect:
                return []
            clauses.append("effect = ?"); params.append(str(effect))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        eff_col = "effect" if self._has_effect else "'' AS effect"
        sql = (f"SELECT ts, kind, user_id, session_id, source, attrs, {eff_col} "
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
                        "session_id": r["session_id"], "source": r["source"],
                        "attrs": attrs, "effect": r["effect"] or ""})
        return out

    def count_by_effect(self, *, since_ts: Optional[float] = None,
                        user_id: Optional[str] = None,
                        attrs_in: Optional[Dict[str, Any]] = None
                        ) -> Optional[Dict[str, int]]:
        """``{effect: n}`` over ``external_write`` rows — a ``GROUP BY``, never a
        row fetch, so a busy window cannot truncate the count.

        ``attrs_in`` filters on envelope attributes, e.g.
        ``{"autonomous": (1,), "outcome": ("ok",)}`` (a JSON boolean compares as
        1/0). Returns ``None`` when the store is unavailable, so a reader can tell
        "nothing happened" from "I could not look".
        """
        if not self._ready or not self._has_effect:
            return None
        from core.event_kinds import EXTERNAL_WRITE
        clauses: List[str] = ["kind = ?"]
        params: List[Any] = [EXTERNAL_WRITE]
        if since_ts is not None:
            clauses.append("ts >= ?"); params.append(float(since_ts))
        if user_id is not None:
            clauses.append("user_id = ?"); params.append(str(user_id))
        for name, values in (attrs_in or {}).items():
            vals = list(values)
            if not vals:
                continue
            clauses.append(f"json_extract(attrs, '$.{name}') IN "
                           f"({', '.join('?' for _ in vals)})")
            params.extend(vals)
        sql = (f"SELECT effect, COUNT(*) AS n FROM telemetry_events "
               f"WHERE {' AND '.join(clauses)} GROUP BY effect")
        try:
            rows = execute_retry(self.db_path, sql, tuple(params), fetch="all") or []
        except Exception as e:
            logger.warning("event_log count_by_effect failed: %s", e, exc_info=True)
            return None
        return {str(r["effect"]): int(r["n"]) for r in rows if r["effect"]}

    def count_where(self, *, kind: Optional[str] = None,
                    user_id: Optional[str] = None,
                    since_ts: Optional[float] = None,
                    sources_in: Optional[Any] = None,
                    sources_not_in: Optional[Any] = None,
                    attrs_in: Optional[Dict[str, Any]] = None,
                    attrs_not_in: Optional[Dict[str, Any]] = None
                    ) -> Optional[int]:
        """``COUNT(*)`` over the same rows :meth:`query` would return.

        The companion of the reader, added because every gate that needs "how
        many X in the last 24h" used ``query(..., limit=1000)`` and counted in
        Python: past a thousand rows in the window the gate silently read a
        TRUNCATED window and under-counted, i.e. it stopped gating exactly when
        traffic was highest (D48, 2026-09-21 interface audit).

        ``attrs_in`` / ``attrs_not_in`` filter on JSON attributes, e.g.
        ``attrs_in={"outcome": ("sent", "fallback")}``. A ``NOT IN`` test is
        NULL-tolerant on purpose: a row written before an attribute existed has
        no opinion about it and must still be counted, where plain SQL ``NOT
        IN`` would drop it.

        Returns ``None`` when the store is unavailable or the query fails, so a
        caller can tell "nothing matched" from "I could not look" and fail open.
        """
        if not self._ready:
            return None
        clauses: List[str] = []
        params: List[Any] = []
        if since_ts is not None:
            clauses.append("ts >= ?"); params.append(float(since_ts))
        if kind is not None:
            clauses.append("kind = ?"); params.append(str(kind))
        if user_id is not None:
            clauses.append("user_id = ?"); params.append(str(user_id))

        def _placeholders(values) -> str:
            return ", ".join("?" for _ in values)

        if sources_in:
            vals = [str(v) for v in sources_in]
            clauses.append(f"source IN ({_placeholders(vals)})"); params.extend(vals)
        if sources_not_in:
            vals = [str(v) for v in sources_not_in]
            clauses.append(f"source NOT IN ({_placeholders(vals)})"); params.extend(vals)
        for name, values in (attrs_in or {}).items():
            vals = [str(v) for v in values]
            if not vals:
                continue
            clauses.append(
                f"json_extract(attrs, '$.{name}') IN ({_placeholders(vals)})")
            params.extend(vals)
        for name, values in (attrs_not_in or {}).items():
            vals = [str(v) for v in values]
            if not vals:
                continue
            clauses.append(
                f"(json_extract(attrs, '$.{name}') IS NULL OR "
                f"json_extract(attrs, '$.{name}') NOT IN ({_placeholders(vals)}))")
            params.extend(vals)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        try:
            row = execute_retry(
                self.db_path,
                f"SELECT COUNT(*) AS n FROM telemetry_events{where}",
                tuple(params), fetch="one")
        except Exception as e:
            logger.warning("event_log count_where failed: %s", e, exc_info=True)
            return None
        if row is None:
            return 0
        try:
            return int(row["n"])
        except Exception:
            return int(row[0])

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
                  user_id: Optional[str] = None,
                  kind: Optional[str] = None) -> Dict[str, Any]:
        """Complete rollup, without the display query's pagination limit.

        Unreadable or malformed financial data raises: callers must display
        unavailable rather than a plausible but incomplete spend total.
        """
        from core.event_kinds import WALLET_SPEND
        if not self._ready:
            raise RuntimeError("Event log unavailable")
        clauses, params = [], [WALLET_SPEND, WALLET_SPEND]
        for column, value, op in (("ts", since_ts, ">="),
                                  ("user_id", user_id, "="), ("kind", kind, "=")):
            if value is not None:
                clauses.append(f"{column} {op} ?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = execute_retry(self.db_path, """
            SELECT kind, COUNT(*) AS n,
              SUM(CASE WHEN kind = ? THEN json_extract(attrs, '$.amount_usd')
                  ELSE 0 END) AS spend,
              SUM(CASE WHEN kind = ? AND (
                    COALESCE(json_type(attrs, '$.amount_usd'), '') NOT IN ('integer', 'real')
                    OR json_extract(attrs, '$.amount_usd') < 0)
                  THEN 1 ELSE 0 END) AS invalid
            FROM telemetry_events
        """ + where + " GROUP BY kind", tuple(params), fetch="all")
        counts = {r["kind"]: int(r["n"]) for r in rows}
        total_spend = sum(float(r["spend"] or 0) for r in rows)
        if any(r["invalid"] for r in rows) or not math.isfinite(total_spend):
            raise ValueError("Event log contains invalid wallet spend")
        return {"counts_by_kind": counts, "wallet_spend_usd": total_spend,
                "total_events": sum(counts.values())}


# --- process-wide singleton keyed by db path -------------------------------------
_INSTANCES: Dict[str, TelemetryEventLog] = {}


def telemetry_db_path(data_dir: Optional[str] = None) -> str:
    """The ONE resolution of where ``telemetry_events.db`` lives.

    Order: an explicit ``TELEMETRY_EVENT_LOG_PATH`` override (the test suite's
    seam), else a file that already exists under *data_dir* (a tenant-local or
    explicitly resolved home), else the shared sidecar path
    (``core.runtime_paths.sidecar_db_path``, read-both/write-new). Every reader
    (status snapshot, security digest, missed notices, recap) and the writer
    singleton resolve through here so they can never disagree about which file
    holds the truth. A read never CREATES the file — see :func:`open_event_log`.
    """
    override = (os.getenv("TELEMETRY_EVENT_LOG_PATH") or "").strip()
    if override:
        return override
    if data_dir:
        local = os.path.join(str(data_dir), "telemetry_events.db")
        if os.path.exists(local):
            return local
    from core.runtime_paths import sidecar_db_path
    return str(sidecar_db_path("telemetry_events.db"))


def open_event_log(data_dir: Optional[str] = None) -> Optional[TelemetryEventLog]:
    """A read-only handle on the resolved log, or None when the file does not
    exist yet. Constructing ``TelemetryEventLog`` creates the file, so a read
    surface must go through here rather than manufacture an empty db in whatever
    home it resolved."""
    path = telemetry_db_path(data_dir)
    if not path or not os.path.exists(path):
        return None
    return TelemetryEventLog(path)


def emit(kind: str, *, source: str, user_id: str = "", session_id: str = "",
         attrs: Optional[Dict[str, Any]] = None) -> None:
    """Fail-open record: the enabled check, the singleton lookup and the
    exception swallow that every emitter used to carry by hand. ``attrs`` is
    an explicit dict — never ``**kwargs`` — because ``record()`` has reserved
    keyword names and a colliding attr would be silently eaten."""
    try:
        if not event_log_enabled():
            return
        get_event_log().record(kind, user_id=str(user_id or ""),
                               session_id=str(session_id or ""),
                               source=source, attrs=attrs or {})
    except Exception:
        logger.debug("event emit skipped (%s from %s)", kind, source, exc_info=True)


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
