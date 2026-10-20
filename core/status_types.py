"""The status snapshot's TYPES and shared literals: the section/health dataclasses,
the state words, the goal-board and telemetry literals, and the small pure
helpers every section builder uses.

Split out of ``core/status_snapshot.py`` (W9, 2026-10-10 structural review)
unchanged. ``core.status_snapshot`` re-exports every name and stays the ONE
assembler; this module imports nothing above it and no section module.
"""
from __future__ import annotations

import logging
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


logger = logging.getLogger("core.status_snapshot")


STATE_OK = "ok"
STATE_DEGRADED = "degraded"
STATE_UNAVAILABLE = "unavailable"

OVERALL_OK = "ok"
OVERALL_DEGRADED = "degraded"
OVERALL_PARTIAL = "partial"

SEVERITY_CRIT = "crit"
SEVERITY_WARN = "warn"

# Goal-board literals — the ONE spelling (core.goal_vocab; the board imports
# the same names, and the contract test still pins them equal).
from core.remedy import flag_command as _flag_command  # noqa: E402
from core.goal_vocab import (  # noqa: E402
    KIND_GOAL as _KIND_GOAL, KIND_ASK as _KIND_ASK,
    STATUS_READY as _ST_READY, STATUS_RUNNING as _ST_RUNNING,
    STATUS_BLOCKED as _ST_BLOCKED, STATUS_WAITING as _ST_WAITING,
    ASK_OPEN as _ASK_OPEN,
    ASK_KIND_TOKEN_IDENTITY, has_own_surface,
)
_TOOL_APPROVAL_ASK_KIND = "tool_approval"
# Telemetry kinds (pinned to core/event_kinds.py by the contract test).
_K_USER_DELIVERY, _K_OWNER_NOTICE = "user_delivery", "owner_notice"
_K_CREDIT_SENTINEL, _K_CRON_RUN, _K_GOAL_RUN = "credit_sentinel", "cron_run", "goal_run"
_K_SELF_WAKE, _K_TOOL_TIMEOUT, _K_TOOL_DENIED = "self_wake", "tool_timeout", "tool_denied"
_K_RUN_DEGRADED, _K_AUTONOMY_TICK = "run_outcome_degraded", "autonomy_tick"
_K_WALLET_SPEND = "wallet_spend"
_K_AUTONOMY_STARTED = "autonomy_started"
_K_SURFACE_POLL_ERROR = "surface_poll_error"
_K_RAIL_PRECONDITION_FAILED = "rail_precondition_failed"
#: 031: outcomes that mean "the starter honoured the pause" (not a violation)
_PAUSE_HONOURED_OUTCOMES = frozenset({"paused", "skipped", "held", "dropped"})
_SUPPRESSED_PREFIX = "[suppressed by daily proactive-message cap"

#: How many undelivered owner-message BODIES the snapshot carries. The rail
#: promises every suppressed message is "rolled into the digest", and prod's
#: digest is the LLM cron job, which reads this snapshot — so a count alone
#: ("16 suppressed, all self_evolution", 2026-09-19) could never surface the one
#: report the owner needed. Bounded because a digest that quotes two hundred
#: bodies is not a digest; whatever is left out is COUNTED in `missed_more`.
MISSED_IN_SNAPSHOT = 8

#: Per-entry cap. Long enough to be quotable, short enough that eight of them
#: cannot bloat every status read.
MISSED_TEXT_CHARS = 400

#: ⚠️ The aggregation gate, not just a display order. `_assemble` iterates THIS
#: tuple, so a section missing from it contributes NO health item and reports no
#: unavailability — it is present in `sections` and silent everywhere that
#: matters. A new section belongs here in the same commit that adds it.
SECTION_ORDER = ("session", "providers", "work", "approvals", "loops",
                 "tools", "knowledge", "delivery", "posture", "security", "identity",
                 "rules", "apps", "groups", "room_actions", "creations", "collectibles",
                 "liquidity", "wallet", "custody", "packs", "money", "economics")


@dataclass
class HealthItem:
    key: str
    text: str
    remedy: str = ""
    severity: str = SEVERITY_WARN


@dataclass
class Section:
    name: str
    state: str = STATE_OK
    reason: str = ""
    lines: List[str] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)
    health: List[HealthItem] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.state != STATE_UNAVAILABLE


@dataclass
class StatusSnapshot:
    user_id: str
    generated_at: float
    window_sec: int
    sections: Dict[str, Section]
    health: List[HealthItem]
    unavailable_sources: List[str]

    @property
    def overall(self) -> str:
        if self.health:
            return OVERALL_DEGRADED
        if self.unavailable_sources:
            return OVERALL_PARTIAL
        return OVERALL_OK

    def section(self, name: str) -> Section:
        return self.sections[name]


def _unavailable(name: str, exc: BaseException) -> Section:
    reason = f"{type(exc).__name__}: {str(exc)[:160]}".strip()
    return Section(name=name, state=STATE_UNAVAILABLE, reason=reason)


def _guarded(name: str, fn, *args, **kwargs) -> Section:
    try:
        sec = fn(*args, **kwargs)
        if sec.health and sec.state == STATE_OK:
            sec.state = STATE_DEGRADED
        return sec
    except Exception as e:  # every reader failure becomes a typed, visible state
        logger.debug("status_snapshot: section %s unavailable: %s", name, e, exc_info=True)
        return _unavailable(name, e)


def _hhmm(ts: Optional[float]) -> str:
    if not ts:
        return "?"
    return time.strftime("%m-%d %H:%MZ", time.gmtime(float(ts)))


def _age(ts: Optional[float], now: float) -> str:
    if not ts:
        return "?"
    d = max(0.0, now - float(ts))
    if d < 3600:
        return f"{int(d // 60)}m"
    if d < 86400:
        return f"{d / 3600:.1f}h"
    return f"{d / 86400:.1f}d"


def _payload(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(row.get("payload") or "{}") or {}
    except Exception:
        return {}


def _telemetry_db_path(data_dir: str) -> str:
    from core.event_log import telemetry_db_path
    return telemetry_db_path(data_dir)


def _tele_rows(tele: Section, kind: str) -> List[Dict[str, Any]]:
    return [r for r in (tele.data.get("rows") or []) if r.get("kind") == kind]


def snapshot_rows(db_path: str, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """``core.status_snapshot._rows``, looked up at CALL time. Every section module
    reads through this, so a test that patches the snapshot's reader reaches
    every builder (the 067 P5a delegator, once instead of per module)."""
    from core import status_snapshot as _ss
    return _ss._rows(db_path, sql, params)
