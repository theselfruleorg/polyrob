"""045 — the ONE security rollup.

Every security surface renders THIS. Adding a second aggregator is the mistake
core/status_snapshot.py's docstring already warns about ("don't add an eighth
renderer — extend the snapshot").

Counts with GROUP BY, never fetches rows: `inbound_routed` is the highest-volume
kind in the store under group load, and a row-fetching rollup would slow every
status surface exactly when the agent is busiest.

Layering: reads sqlite read-only through core.sqlite_util, like
core/status_snapshot.py — core may not import agents.* or surfaces.*.
"""
import os
from dataclasses import dataclass, field
from typing import List, Tuple

_TOP_N = 5

#: The ONE producer `refused` counts: ``core/security/refusals.py``'s typed
#: emit. ``tools/controller/execution.py`` has ALSO emitted ``tool_denied`` for
#: every pre-tool-call hook veto since 2026-07-04, with the full 200-char denial
#: prose as its ``reason``. The correspondent gate and the approval hook are
#: both pre-tool-call hooks, so ONE correspondent-taint refusal writes 2 rows and
#: one forged-turn owner-queue refusal writes 3 — an inflation factor that
#: varies by refusal — and the reason ranking grouped prose strings beside the
#: typed slugs. Narrowing the READER is the smaller, safer change: the untyped
#: emit predates this work and other consumers (the activity feed, the status
#: snapshot's telemetry rows) still read it.
GATE_SOURCE = "gate"


@dataclass
class SecurityRollup:
    window_sec: int
    inbound: int = 0
    denied: int = 0
    refused: int = 0
    flagged: int = 0
    rate_limited: int = 0
    top_senders: List[Tuple[str, int]] = field(default_factory=list)
    top_denial_reasons: List[Tuple[str, int]] = field(default_factory=list)
    top_refusal_reasons: List[Tuple[str, int]] = field(default_factory=list)


def _db_path(data_dir: str) -> str:
    override = (os.getenv("TELEMETRY_EVENT_LOG_PATH") or "").strip()
    if override:
        return override
    local = os.path.join(data_dir, "telemetry_events.db")
    if os.path.exists(local):
        return local
    from core.runtime_paths import sidecar_db_path
    return str(sidecar_db_path("telemetry_events.db"))


def _query(db_path: str, sql: str, params: tuple):
    if not db_path or not os.path.exists(db_path):
        raise FileNotFoundError(
            f"{os.path.basename(db_path or '?')} not found at {db_path}")
    from core.sqlite_util import execute_retry
    return [dict(r) for r in (execute_retry(db_path, sql, params, fetch="all") or [])]


def _counts_by_kind(db_path: str, user_id: str, since_ts: float) -> dict:
    rows = _query(
        db_path,
        "SELECT kind, COUNT(*) AS n FROM telemetry_events "
        "WHERE ts >= ? AND user_id = ? GROUP BY kind",
        (float(since_ts), str(user_id)))
    return {r["kind"]: int(r["n"]) for r in rows}


def _count_kind(db_path: str, user_id: str, since_ts: float, kind: str,
                source: str) -> int:
    """Count ONE kind narrowed to ONE producer. See ``GATE_SOURCE``."""
    rows = _query(
        db_path,
        "SELECT COUNT(*) AS n FROM telemetry_events "
        "WHERE ts >= ? AND user_id = ? AND kind = ? AND source = ?",
        (float(since_ts), str(user_id), kind, source))
    return int(rows[0]["n"]) if rows else 0


def _top_attr(db_path: str, user_id: str, since_ts: float, kind: str,
              attr: str, source: str = "") -> List[Tuple[str, int]]:
    """Rank one attrs key within a SINGLE kind. json_extract keeps the
    aggregation in sqlite rather than pulling every row into Python. The
    `n DESC, v ASC` order makes ties deterministic across runs.

    ``source`` narrows to one producer (empty = every producer)."""
    extra = " AND source = ?" if source else ""
    params: tuple = (f"$.{attr}", float(since_ts), str(user_id), kind)
    if source:
        params += (source,)
    rows = _query(
        db_path,
        "SELECT json_extract(attrs, ?) AS v, COUNT(*) AS n FROM telemetry_events "
        "WHERE ts >= ? AND user_id = ? AND kind = ? AND v IS NOT NULL" + extra +
        " GROUP BY v ORDER BY n DESC, v ASC LIMIT ?",
        params + (_TOP_N,))
    return [(str(r["v"]), int(r["n"])) for r in rows]


def _top_senders(db_path: str, user_id: str, since_ts: float,
                  kinds: Tuple[str, ...], attr: str) -> List[Tuple[str, int]]:
    """Rank one attrs key ACROSS several kinds in ONE query.

    A sender who is moderate in every kind but never a top-N leader in any
    single one must not be invisible to the combined ranking — truncating
    each kind's list to N before merging in Python drops exactly that
    sender (a both-lanes sender can rank above a single-lane sender by true
    total while falling outside each lane's individual cut). Grouping by
    `v` alone over the `kind IN (...)` union sums counts across kinds
    server-side, so the true combined total is what gets ranked and cut —
    never a merge of two already-truncated lists.
    """
    placeholders = ",".join("?" for _ in kinds)
    rows = _query(
        db_path,
        "SELECT json_extract(attrs, ?) AS v, COUNT(*) AS n FROM telemetry_events "
        f"WHERE ts >= ? AND user_id = ? AND kind IN ({placeholders}) "
        "AND v IS NOT NULL GROUP BY v ORDER BY n DESC, v ASC LIMIT ?",
        (f"$.{attr}", float(since_ts), str(user_id), *kinds, _TOP_N))
    return [(str(r["v"]), int(r["n"])) for r in rows]


def build_security_rollup(user_id: str, *, data_dir: str, since_ts: float,
                          window_sec: int) -> SecurityRollup:
    """Aggregate the opsec lanes over one window. Raises when the store is
    absent — a status view must never CREATE a store, and 'no store' must never
    render as 'zero events'.

    ⚠️ Deliberate narrowing: ``refused`` (and ``top_refusal_reasons``) count the
    TYPED gate refusals only — ``source='gate'``, i.e. what
    ``core/security/refusals.py`` writes — not every ``tool_denied`` row in the
    store. See :data:`GATE_SOURCE`: the controller's untyped hook-veto emit
    multiplies one refusal into two or three rows and carries denial prose where
    a slug belongs, so counting it would inflate ``refused`` by a factor that
    varies per refusal. Every other lane counts every row of its kind.
    """
    from core.event_kinds import (ACCESS_DENIED, INBOUND_ROUTED,
                                  INJECTION_FLAGGED, TOOL_DENIED)
    db = _db_path(data_dir)
    uid = str(user_id or "")
    by_kind = _counts_by_kind(db, uid, since_ts)
    r = SecurityRollup(
        window_sec=int(window_sec),
        inbound=by_kind.get(INBOUND_ROUTED, 0),
        denied=by_kind.get(ACCESS_DENIED, 0),
        refused=_count_kind(db, uid, since_ts, TOOL_DENIED, GATE_SOURCE),
        flagged=by_kind.get(INJECTION_FLAGGED, 0),
        # rate_limited stays 0 in Phase 1: lane 4 (per-speaker cost and abuse)
        # is Phase 6 and nothing emits a limiter trip yet. The field exists so
        # the consumers do not change shape later; 0 here means NOT MEASURED,
        # not "no trips" — ruling R-1.
        rate_limited=0,
    )
    r.top_denial_reasons = _top_attr(db, uid, since_ts, ACCESS_DENIED, "reason")
    r.top_refusal_reasons = _top_attr(db, uid, since_ts, TOOL_DENIED, "reason",
                                      source=GATE_SOURCE)
    r.top_senders = _top_senders(
        db, uid, since_ts, (ACCESS_DENIED, INBOUND_ROUTED), "sender")
    return r
