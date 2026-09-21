"""Durable external-rail verdicts (056 WS4 → 057 WS-F).

A rejected credential is a fact several tiers need without importing each other:
`tools.email_tool` learns of a 535 on login; `agents.task.constants` must stop
REQUESTING the email tool while that rejection is fresh; `core.status_snapshot`
renders it as a health line (core never imports tools).

WS-F (057 R5) makes the register DURABLE. Before it, the verdict lived in a
process-local dict keyed on ``time.monotonic()``, so:

- every restart forgot it and re-probed a dead rail (1,439 email ERROR lines in
  24 h on prod, 932 "535" lines, 317 X-API "402" lines);
- the three service units that share one data dir each kept their own copy;
- status could only say "last seen HH:MM" (the latest re-probe), never
  "since <first failure>", because nothing carried a first_seen.

So the register is a small SQLite store (``verdicts.db``, WAL + jittered retry
via :mod:`core.sqlite_util`, listed in :mod:`core.db_manifest`) keyed
``(kind, key)`` and timestamped with WALL CLOCK — ``time.monotonic()`` has no
meaning across a restart. A success CLEARS the row; a repeat failure bumps
``last_seen``/``count`` and KEEPS ``first_seen``, which is what "SMTP 535 since
09-15 (5d)" reads from.

Fail-open: an unreadable/unwritable store degrades to the legacy in-process
dict and logs ONCE. A verdict is an optimisation (don't re-probe a dead rail) —
losing it must never fail a call closed.

Layering: core-tier, stdlib + ``core.sqlite_util`` / ``core.runtime_paths`` only.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from core.runtime_paths import data_home_db_path
from core.sqlite_util import execute_retry, init_schema

logger = logging.getLogger("core.credential_verdicts")

# --- one TTL constant per kind, HERE (it used to be a magic 900 duplicated across
# tools/email_tool.py and agents/task/constants.py, i.e. across a layering
# boundary). The TTL is the RE-PROBE backoff: how long a refusal answers from
# memory before the rail is tried again.
SMTP_TTL_SEC = 900.0          # ~4 bad logins/hour per rail instead of one a minute
IMAP_TTL_SEC = 900.0          # the RECEIVE half of the same mailbox, same cadence
TWITTER_API_TTL_SEC = 3600.0  # a 402 (credits depleted) is not fixed in a minute
MISSING_KEY_TTL_SEC = 86400.0 # an absent API key is an owner action, not a retry

DEFAULT_TTL_BY_KIND: Dict[str, float] = {
    "smtp": SMTP_TTL_SEC,
    "imap": IMAP_TTL_SEC,
    "twitter_api": TWITTER_API_TTL_SEC,
    "missing_key": MISSING_KEY_TTL_SEC,
}

#: Kinds whose verdict only an OWNER ACTION clears — a new app password, a
#: topped-up account, a key that gets set. Nothing in the system re-probes them
#: on its own once the rail is dropped from the autonomous toolset, so their
#: ``last_seen`` stops advancing.
#:
#: D68 (2026-09-21 interface audit): :func:`_prune` deleted any row idle for 30
#: days, which is exactly what a STANDING unfixed rejection looks like. The
#: verdict vanished, the status line with it, and the rail silently came back
#: into the autonomous toolset while the credential was still dead. A verdict
#: with an open remedy is history only once the owner closes it.
OPEN_REMEDY_KINDS = frozenset({"smtp", "imap", "twitter_api", "missing_key"})

# The TTL is the BASE hold; every repeat failure doubles it, up to this cap
# (2026-09-19, intel inbox): a flat 900 s was shorter than the 20–35 min cron
# cadence, so a password revoked for days was still re-probed — and its
# traceback chain re-logged — on every run. count 1 → ttl, 2 → 2·ttl, …, 6+ → cap.
BACKOFF_CAP_SEC = 6 * 3600.0


def hold_sec(ttl_sec: Optional[float], count: int) -> Optional[float]:
    """Seconds a verdict answers from memory after ``count`` consecutive failures.
    ``None`` = no TTL = never lapses."""
    if ttl_sec is None or float(ttl_sec) <= 0:
        return None
    n = max(int(count or 1), 1)
    return float(min(float(ttl_sec) * (2 ** (n - 1)), BACKOFF_CAP_SEC))


def credential_digest(value: Optional[str]) -> str:
    """A change DETECTOR for a secret, never a display form: a salted 12-hex
    sha256. A verdict keyed on it belongs to ONE credential, so a new app
    password has no standing verdict and is probed at once — the owner's fix
    never waits out a hold (`core.security.redaction.fingerprint` reveals a
    prefix and suffix and is for display, not for this)."""
    if not value:
        return ""
    import hashlib
    return hashlib.sha256(b"polyrob-credential-verdict:" + str(value).encode("utf-8")).hexdigest()[:12]

# Rows whose last_seen is older than this are dropped on the next write: a rail
# nobody has touched in a month is history, not a live verdict.
_RETENTION_SEC = 30 * 86400.0

_DB_NAME = "verdicts.db"
_DB_ENV_KEY = "VERDICTS_DB_PATH"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    count      INTEGER NOT NULL DEFAULT 1,
    code       TEXT,
    remedy     TEXT,
    ttl_sec    REAL,
    PRIMARY KEY (kind, key)
);
CREATE INDEX IF NOT EXISTS idx_verdicts_kind ON verdicts(kind, last_seen);
"""


@dataclass(frozen=True)
class Verdict:
    """One rail's standing refusal. ``first_seen`` is the honest "since"."""

    kind: str
    key: str
    first_seen: float
    last_seen: float
    count: int
    code: Optional[str] = None
    remedy: Optional[str] = None
    ttl_sec: Optional[float] = None

    @property
    def age_sec(self) -> float:
        """Seconds since the FIRST failure (what a renderer means by "since")."""
        return max(0.0, time.time() - float(self.first_seen))

    @property
    def idle_sec(self) -> float:
        """Seconds since the last observed failure (the re-probe clock)."""
        return max(0.0, time.time() - float(self.last_seen))

    @property
    def hold_sec(self) -> Optional[float]:
        """The CURRENT re-probe hold: the TTL doubled per repeat failure, capped."""
        return hold_sec(self.ttl_sec, self.count)

    @property
    def remaining_sec(self) -> float:
        """Seconds until the rail is due another probe (0 when lapsed or no TTL)."""
        hold = self.hold_sec
        if hold is None:
            return 0.0
        return max(0.0, hold - self.idle_sec)

    @property
    def live(self) -> bool:
        """True while the re-probe backoff has not lapsed.

        A verdict with no TTL never lapses. A LAPSED verdict is not "resolved"
        — only a success clears a row — it just means the rail is due another
        attempt, which is why :func:`active` still reports it by default.
        """
        hold = self.hold_sec
        if hold is None:
            return True
        return self.idle_sec < hold


# --- fail-open fallback ---------------------------------------------------------
_FALLBACK: Dict[Tuple[str, str], Verdict] = {}
_DEGRADED_LOGGED = False
_WARNED: set = set()


def _degrade(exc: BaseException) -> None:
    """Fall back to the in-process dict and say so ONCE."""
    global _DEGRADED_LOGGED
    if not _DEGRADED_LOGGED:
        _DEGRADED_LOGGED = True
        logger.warning(
            "credential verdicts store unavailable (%s) — verdicts are process-local "
            "for the rest of this process and will not survive a restart", exc)


_READY: Dict[str, bool] = {}


def _db(*, create: bool = True) -> Optional[str]:
    """Resolve + schema-init the store. ``None`` means "use the fallback".

    ``create=False`` is the READ path: a read never plants ``verdicts.db`` in a
    data home (``polyrob doctor`` in an empty directory must leave it empty, and
    the status SSOT rule is that a store is never CREATED by a read). An absent
    file simply means "no standing verdict"."""
    try:
        path = data_home_db_path(_DB_NAME, env_key=_DB_ENV_KEY)
    except Exception as e:  # pragma: no cover - path resolution is defensive
        _degrade(e)
        return None
    if not create and not _READY.get(path) and not os.path.exists(path):
        return None
    if not _READY.get(path):
        try:
            init_schema(path, _SCHEMA, mkdir=True)
            _READY[path] = True
        except Exception as e:
            _degrade(e)
            return None
    return path


def _row_to_verdict(row) -> Verdict:
    return Verdict(
        kind=str(row["kind"]),
        key=str(row["key"] or ""),
        first_seen=float(row["first_seen"]),
        last_seen=float(row["last_seen"]),
        count=int(row["count"] or 1),
        code=(str(row["code"]) if row["code"] is not None else None),
        remedy=(str(row["remedy"]) if row["remedy"] is not None else None),
        ttl_sec=(float(row["ttl_sec"]) if row["ttl_sec"] is not None else None),
    )


# --- public API -----------------------------------------------------------------
def record_rejection(kind: str, key: str = "", *, code: Optional[str] = None,
                     remedy: Optional[str] = None,
                     ttl_sec: Optional[float] = None) -> Verdict:
    """Remember that ``(kind, key)`` was rejected, now.

    A repeat keeps ``first_seen`` and bumps ``last_seen``/``count`` — the outage
    started when it started. Returns the stored verdict, so a caller can emit a
    durable event only on a FRESH one (``verdict.count == 1``).
    """
    kind = str(kind)
    key = str(key or "")
    now = time.time()
    ttl = float(ttl_sec) if ttl_sec is not None else DEFAULT_TTL_BY_KIND.get(kind)
    path = _db()
    if path:
        try:
            execute_retry(
                path,
                "INSERT INTO verdicts (kind, key, first_seen, last_seen, count, code, remedy, ttl_sec) "
                "VALUES (?, ?, ?, ?, 1, ?, ?, ?) "
                "ON CONFLICT(kind, key) DO UPDATE SET "
                "  last_seen = excluded.last_seen, "
                "  count = verdicts.count + 1, "
                "  code = COALESCE(excluded.code, verdicts.code), "
                "  remedy = COALESCE(excluded.remedy, verdicts.remedy), "
                "  ttl_sec = COALESCE(excluded.ttl_sec, verdicts.ttl_sec)",
                (kind, key, now, now, code, remedy, ttl),
            )
            _prune(path)
            stored = verdict(kind, key)
            if stored is not None:
                return stored
        except (sqlite3.Error, OSError) as e:
            _degrade(e)
    prev = _FALLBACK.get((kind, key))
    v = Verdict(kind=kind, key=key,
                first_seen=(prev.first_seen if prev else now), last_seen=now,
                count=((prev.count + 1) if prev else 1),
                code=(code if code is not None else (prev.code if prev else None)),
                remedy=(remedy if remedy is not None else (prev.remedy if prev else None)),
                ttl_sec=(ttl if ttl is not None else (prev.ttl_sec if prev else None)))
    _FALLBACK[(kind, key)] = v
    return v


def clear_rejection(kind: str, key: str = "") -> None:
    """Forget one verdict — what a SUCCESS on that rail means."""
    kind, key = str(kind), str(key or "")
    _FALLBACK.pop((kind, key), None)
    path = _db(create=False)
    if not path:
        return
    try:
        execute_retry(path, "DELETE FROM verdicts WHERE kind = ? AND key = ?", (kind, key))
    except (sqlite3.Error, OSError) as e:
        _degrade(e)


def clear_kind(kind: str) -> None:
    """Forget every verdict of one kind (the dict-shaped ``.clear()`` seam)."""
    kind = str(kind)
    for k in [k for k in _FALLBACK if k[0] == kind]:
        _FALLBACK.pop(k, None)
    path = _db(create=False)
    if not path:
        return
    try:
        execute_retry(path, "DELETE FROM verdicts WHERE kind = ?", (kind,))
    except (sqlite3.Error, OSError) as e:
        _degrade(e)


def rejected_within(kind: str, seconds: float) -> bool:
    """True while ANY key of ``kind`` is inside its hold.

    ``seconds`` is the BASE window; the effective hold grows with each repeat
    failure (:func:`hold_sec`), so a caller keeps its one constant and still
    stops re-probing a rail that has been dead for days.
    """
    now = time.time()
    base = float(seconds)

    def _inside(last_seen: float, count: int) -> bool:
        return (now - float(last_seen)) < (hold_sec(base, count) or 0.0)

    path = _db(create=False)
    if path:
        try:
            rows = execute_retry(
                path,
                "SELECT last_seen, count FROM verdicts WHERE kind = ? AND last_seen > ?",
                (str(kind), now - BACKOFF_CAP_SEC), fetch="all") or []
            if any(_inside(r["last_seen"], r["count"]) for r in rows):
                return True
        except (sqlite3.Error, OSError) as e:
            _degrade(e)
    return any(_inside(v.last_seen, v.count)
               for (k, _), v in _FALLBACK.items() if k == str(kind))


def verdict(kind: str, key: str = "") -> Optional[Verdict]:
    """The stored verdict for one exact ``(kind, key)``, or None."""
    kind, key = str(kind), str(key or "")
    path = _db(create=False)
    if path:
        try:
            row = execute_retry(
                path,
                "SELECT kind, key, first_seen, last_seen, count, code, remedy, ttl_sec "
                "FROM verdicts WHERE kind = ? AND key = ?", (kind, key), fetch="one")
            if row is not None:
                return _row_to_verdict(row)
        except (sqlite3.Error, OSError) as e:
            _degrade(e)
    return _FALLBACK.get((kind, key))


def active(kind: Optional[str] = None, *, live_only: bool = False) -> List[Verdict]:
    """Every standing verdict (newest failure first) — the renderer's seam.

    A row exists only while the rail is UNRESOLVED (a success deletes it), so
    the default includes verdicts whose re-probe backoff has lapsed: an old
    verdict must not be hidden just because nothing has retried the rail
    recently. Pass ``live_only=True`` for "still inside its backoff".
    """
    out: List[Verdict] = []
    path = _db(create=False)
    if path:
        try:
            sql = ("SELECT kind, key, first_seen, last_seen, count, code, remedy, ttl_sec "
                   "FROM verdicts")
            params: tuple = ()
            if kind:
                sql += " WHERE kind = ?"
                params = (str(kind),)
            sql += " ORDER BY last_seen DESC"
            rows = execute_retry(path, sql, params, fetch="all") or []
            out = [_row_to_verdict(r) for r in rows]
        except (sqlite3.Error, OSError) as e:
            _degrade(e)
    if not out:
        out = sorted(
            (v for (k, _), v in _FALLBACK.items() if not kind or k == str(kind)),
            key=lambda v: v.last_seen, reverse=True)
    if live_only:
        out = [v for v in out if v.live]
    return out


def since_text(v: Verdict) -> str:
    """``"09-19 10:24Z (8h)"`` — the "since <first failure>" a status line wants."""
    try:
        stamp = time.strftime("%m-%d %H:%MZ", time.gmtime(float(v.first_seen)))
    except (ValueError, OSError, OverflowError):  # pragma: no cover - clock defence
        return "unknown"
    return f"{stamp} ({duration_text(v.age_sec)})"


def duration_text(seconds: float) -> str:
    """Compact age: ``45s`` / ``12m`` / ``8h`` / ``5d``."""
    s = max(0.0, float(seconds))
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s // 60)}m"
    if s < 48 * 3600:
        return f"{int(s // 3600)}h"
    return f"{int(s // 86400)}d"


def warn_once(kind: str, key: str = "", *, episode: Optional[float] = None) -> bool:
    """True the FIRST time this process is asked about this verdict episode.

    The 057 R5 complaint is volume, not visibility: a dead rail logged an ERROR
    on every call. Callers gate their WARN on this, so one outage costs one line
    per process — and a NEW episode (a different ``first_seen``) logs again.
    """
    token = (str(kind), str(key or ""), round(float(episode), 3) if episode else None)
    if token in _WARNED:
        return False
    _WARNED.add(token)
    return True


def _prune(path: str) -> None:
    """Drop rows nobody has touched in a month — EXCEPT the open-remedy kinds.

    See :data:`OPEN_REMEDY_KINDS`: a rejection only the owner can clear has no
    re-prober, so age is not evidence that it is over.
    """
    kinds = tuple(sorted(OPEN_REMEDY_KINDS))
    placeholders = ", ".join("?" for _ in kinds)
    try:
        execute_retry(
            path,
            f"DELETE FROM verdicts WHERE last_seen < ? AND kind NOT IN ({placeholders})",
            (time.time() - _RETENTION_SEC, *kinds))
    except (sqlite3.Error, OSError):
        pass


def _reset_for_tests() -> None:
    """Clear process memory and any EXISTING store. Never CREATES the db.

    The test suite points ``VERDICTS_DB_PATH`` at a tmp dir; creating the file
    eagerly would plant a stray ``verdicts.db`` in every test's ``tmp_path``,
    which directory-walking tests then see.
    """
    global _DEGRADED_LOGGED
    _FALLBACK.clear()
    _WARNED.clear()
    _READY.clear()
    _DEGRADED_LOGGED = False
    try:
        path = data_home_db_path(_DB_NAME, env_key=_DB_ENV_KEY)
    except Exception:
        return
    if os.path.exists(path):
        try:
            execute_retry(path, "DELETE FROM verdicts")
        except (sqlite3.Error, OSError):
            pass


__all__ = [
    "MISSING_KEY_TTL_SEC",
    "SMTP_TTL_SEC",
    "TWITTER_API_TTL_SEC",
    "DEFAULT_TTL_BY_KIND",
    "Verdict",
    "active",
    "clear_kind",
    "clear_rejection",
    "duration_text",
    "record_rejection",
    "rejected_within",
    "since_text",
    "verdict",
    "warn_once",
]
