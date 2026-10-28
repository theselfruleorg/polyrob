"""The trading mandate — the owner's card-confirmed record a trading run belongs to (086 §3.1).

One typed row per mandate in ``trading.db`` (``core/db_manifest.py``), table
``trading_mandates``. One NON-STOPPED mandate per user in v1 (a partial unique index
holds it). The mandate holds an allocation and preferences — never a cap: the caps
live only in the signer envelope.

Who may do what:

* :func:`create` — the owner seat only (``actor == "owner_seat"``, stamped by the card
  Confirm).
* :func:`set_status` — anyone may PAUSE (it only removes entries); only the owner
  resumes or stops. ``halted`` is reached only through :func:`latch`.
* :func:`latch` — a breaker (W4's lifecycle pass) halts entries; the latch persists
  across restarts and only :func:`release` (owner) clears it.
* :func:`set_cooldown` — the consecutive-losses breaker: a pause that EXPIRES.

⚠️ An unreadable store is not an empty one: every read raises
:class:`MandateUnreadable` when the file exists and cannot be read. ``None`` means
"there is no such mandate" and nothing else. A READ never creates the file.

How a run reaches its mandate: a job created for a mandate carries
``payload["mandate"] = <id>``; the cron runner / goal dispatcher copy it to the
session request, and the step loop puts it on
``ActionExecutionContext.metadata["mandate"]`` (:data:`METADATA_KEY`), which the money
kernel's mandate step reads (``core.money.mandate_gate``).

Tier-0: imports nothing above ``core``.
"""
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from core.trading_presets import STAGES, Preset, PresetError, default_ticket_usd, get_preset

DB_NAME = "trading.db"
from core.money.hooks import MANDATE_METADATA_KEY as METADATA_KEY

#: The run payload key and the execution-context metadata key that carry a mandate id.
PAYLOAD_KEY = METADATA_KEY

OWNER_SEAT = "owner_seat"

ACTIVE, PAUSED, HALTED, STOPPED = "active", "paused", "halted", "stopped"
STATUSES = (ACTIVE, PAUSED, HALTED, STOPPED)

DEFAULT_REPORT = {"events": ["fills", "exits", "halts"], "digest": "daily"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trading_mandates (
    id             TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    status         TEXT NOT NULL,
    budget_usd     REAL NOT NULL,
    chains         TEXT NOT NULL,
    preset         TEXT NOT NULL,
    ticket_usd     REAL NOT NULL,
    slots          INTEGER NOT NULL,
    breakers       TEXT NOT NULL DEFAULT '{}',
    exit_preset    TEXT,
    report         TEXT NOT NULL DEFAULT '{}',
    post_public    INTEGER NOT NULL DEFAULT 0,
    stage          TEXT NOT NULL DEFAULT 'probe',
    actor          TEXT NOT NULL,
    halted_reason  TEXT,
    halted_at      REAL,
    cooldown_at    REAL,
    cooldown_until REAL,
    released_at    REAL,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    stopped_by     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trading_mandates_one_live
    ON trading_mandates(user_id) WHERE status != 'stopped';
CREATE TABLE IF NOT EXISTS mandate_entries (
    mandate_id TEXT NOT NULL,
    ts         REAL NOT NULL,
    chain      TEXT NOT NULL,
    token      TEXT NOT NULL,
    usd        REAL
);
CREATE INDEX IF NOT EXISTS idx_mandate_entries ON mandate_entries(mandate_id, ts);
"""


class MandateUnreadable(RuntimeError):
    """The store exists and could not be read. Never read this as "no mandate"."""


class MandateError(ValueError):
    """A write the rules refuse (wrong actor, bad field, a second live mandate)."""


@dataclass(frozen=True)
class Mandate:
    id: str
    user_id: str
    status: str
    budget_usd: float
    chains: Tuple[str, ...]
    preset: str
    ticket_usd: float
    slots: int
    actor: str
    created_at: float
    updated_at: float
    breakers: Mapping[str, Any] = field(default_factory=dict)
    exit_preset: Optional[str] = None
    report: Mapping[str, Any] = field(default_factory=dict)
    post_public: bool = False
    stage: str = "probe"
    halted_reason: Optional[str] = None
    halted_at: Optional[float] = None
    cooldown_at: Optional[float] = None
    cooldown_until: Optional[float] = None
    released_at: Optional[float] = None
    stopped_by: Optional[str] = None

    def effective_preset(self) -> Preset:
        """The named preset with this mandate's owner-tuned numbers applied."""
        from core.trading_presets import owner_tune
        overrides = dict(self.breakers or {})
        overrides.setdefault("ticket_usd", self.ticket_usd)
        overrides.setdefault("slots", self.slots)
        return owner_tune(get_preset(self.preset), **overrides)


# -- paths + connections ---------------------------------------------------------

def db_path(data_dir: Optional[str] = None) -> str:
    """``<data_home>/trading.db``, resolved at CALL time."""
    from core.runtime_paths import data_home_db_path
    return data_home_db_path(DB_NAME, data_dir=data_dir)


def init_schema(path: Optional[str] = None) -> str:
    """Create the store (the WRITE path only). Returns the path."""
    from core.sqlite_util import init_schema as _init
    p = path or db_path()
    _init(p, _SCHEMA, mkdir=True,
          columns={"trading_mandates": (("released_at", "REAL"),)})
    return p


def _read(path: Optional[str], sql: str, args: tuple, fetch: str):
    """A read that never creates the file: absent = ``None``/``[]``; an existing file
    that cannot be read raises :class:`MandateUnreadable`."""
    p = path or db_path()
    try:
        os.stat(p)
    except FileNotFoundError:
        return [] if fetch == "all" else None
    except OSError as exc:
        raise MandateUnreadable(f"trading store unreachable: {exc}") from exc
    try:
        from core.sqlite_util import execute_retry
        return execute_retry(p, sql, args, fetch=fetch)
    except Exception as exc:
        raise MandateUnreadable(f"trading store unreadable: {exc}") from exc


def _row_to_mandate(r) -> Mandate:
    return Mandate(
        id=r["id"], user_id=r["user_id"], status=r["status"],
        budget_usd=float(r["budget_usd"]), chains=tuple(json.loads(r["chains"] or "[]")),
        preset=r["preset"], ticket_usd=float(r["ticket_usd"]), slots=int(r["slots"]),
        actor=r["actor"], created_at=float(r["created_at"]),
        updated_at=float(r["updated_at"]),
        breakers=json.loads(r["breakers"] or "{}"), exit_preset=r["exit_preset"],
        report=json.loads(r["report"] or "{}"), post_public=bool(r["post_public"]),
        stage=r["stage"] or "probe", halted_reason=r["halted_reason"],
        halted_at=r["halted_at"], cooldown_at=r["cooldown_at"],
        cooldown_until=r["cooldown_until"], stopped_by=r["stopped_by"],
        released_at=r["released_at"] if "released_at" in r.keys() else None)


# -- reads --------------------------------------------------------------------------

def get(mandate_id: str, *, path: Optional[str] = None) -> Optional[Mandate]:
    """The mandate, ``None`` when there is no such row. Raises :class:`MandateUnreadable`."""
    r = _read(path, "SELECT * FROM trading_mandates WHERE id=?", (str(mandate_id),), "one")
    return _row_to_mandate(r) if r else None


def get_active(user_id: str, *, path: Optional[str] = None) -> Optional[Mandate]:
    """The user's one NON-STOPPED mandate (active, paused or halted), or ``None``."""
    r = _read(path, "SELECT * FROM trading_mandates WHERE user_id=? AND status != ? "
              "ORDER BY created_at DESC LIMIT 1", (str(user_id), STOPPED), "one")
    return _row_to_mandate(r) if r else None


def live_mandates(*, path: Optional[str] = None) -> List[Mandate]:
    """Every NON-STOPPED mandate of every user (the lifecycle pass reads it)."""
    rows = _read(path, "SELECT * FROM trading_mandates WHERE status != ? ORDER BY created_at",
                 (STOPPED,), "all") or []
    return [_row_to_mandate(r) for r in rows]


def entries_since(mandate_id: str, since_ts: float, *, path: Optional[str] = None) -> int:
    """How many entries :func:`record_entry` booked for the mandate since *since_ts*."""
    r = _read(path, "SELECT COUNT(*) AS n FROM mandate_entries WHERE mandate_id=? AND ts>=?",
              (str(mandate_id), float(since_ts)), "one")
    return int(r["n"]) if r else 0


# -- writes -------------------------------------------------------------------------

def _clean_chains(chains: Iterable[str]) -> Tuple[str, ...]:
    if isinstance(chains, str):
        chains = [chains]
    out: List[str] = []
    for c in chains or ():
        c = str(c or "").strip().lower()
        if c and c not in out:
            out.append(c)
    if not out:
        raise MandateError("a mandate needs at least one chain")
    return tuple(out)


def draft(user_id: str, *, budget_usd: float, chains: Sequence[str],
          preset: str = "memecoin", ticket_usd: Optional[float] = None,
          slots: Optional[int] = None, breakers: Optional[Mapping[str, Any]] = None,
          exit_preset: Optional[str] = None, report: Optional[Mapping[str, Any]] = None,
          post_public: bool = False, now: Optional[float] = None,
          mandate_id: Optional[str] = None) -> Mandate:
    """The mandate these fields WOULD make, validated by the same rules as
    :func:`create`, written nowhere (the intake card's echo, ``actor=""``).
    Raises :class:`MandateError`."""
    uid = str(user_id or "").strip()
    if not uid:
        raise MandateError("a mandate needs a user")
    try:
        budget = float(budget_usd)
    except (TypeError, ValueError):
        raise MandateError(f"budget_usd must be a number (got {budget_usd!r})") from None
    if not budget > 0:
        raise MandateError("budget_usd must be > 0")
    try:
        base = get_preset(preset)
    except PresetError as exc:
        raise MandateError(str(exc)) from None
    ticket = float(ticket_usd) if ticket_usd is not None else default_ticket_usd(base, budget)
    n_slots = int(slots) if slots is not None else int(base.slots)
    if not 0 < ticket <= budget:
        raise MandateError("ticket_usd must be > 0 and no more than the allocation")
    tuned = dict(breakers or {})
    if "ticket_usd" in tuned or "slots" in tuned:
        raise MandateError("ticket_usd and slots are mandate fields, not breaker overrides")
    from core.trading_presets import owner_tune
    try:
        owner_tune(base, ticket_usd=ticket, slots=n_slots, **tuned)
    except PresetError as exc:
        raise MandateError(str(exc)) from None
    exit_name = exit_preset or (base.exit.name if base.exit else None)
    if exit_name is not None:
        from core.trading_presets import EXIT_PRESETS
        if exit_name not in EXIT_PRESETS:
            raise MandateError(f"unknown exit preset {exit_name!r}")
    ts = time.time() if now is None else float(now)
    m = Mandate(
        id=mandate_id or f"m_{uuid.uuid4().hex[:12]}", user_id=uid, status=ACTIVE,
        budget_usd=budget, chains=_clean_chains(chains), preset=base.name,
        ticket_usd=ticket, slots=n_slots, actor="", created_at=ts, updated_at=ts,
        breakers=tuned, exit_preset=exit_name,
        report=dict(report if report is not None else DEFAULT_REPORT),
        post_public=bool(post_public), stage="probe")
    return m


def create(user_id: str, *, actor: str, budget_usd: float, chains: Sequence[str],
           preset: str = "memecoin", ticket_usd: Optional[float] = None,
           slots: Optional[int] = None, breakers: Optional[Mapping[str, Any]] = None,
           exit_preset: Optional[str] = None, report: Optional[Mapping[str, Any]] = None,
           post_public: bool = False, path: Optional[str] = None,
           now: Optional[float] = None, mandate_id: Optional[str] = None) -> Mandate:
    """Write a new ACTIVE mandate. Owner seat only; one live mandate per user."""
    if actor != OWNER_SEAT:
        raise MandateError("only the owner seat creates a trading mandate (the card Confirm)")
    from dataclasses import replace
    m = replace(draft(user_id, budget_usd=budget_usd, chains=chains, preset=preset,
                      ticket_usd=ticket_usd, slots=slots, breakers=breakers,
                      exit_preset=exit_preset, report=report, post_public=post_public,
                      now=now, mandate_id=mandate_id), actor=actor)
    p = init_schema(path)
    import sqlite3
    from core.sqlite_util import execute_retry
    try:
        execute_retry(
            p, "INSERT INTO trading_mandates (id, user_id, status, budget_usd, chains, preset, "
            "ticket_usd, slots, breakers, exit_preset, report, post_public, stage, actor, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (m.id, m.user_id, m.status, m.budget_usd, json.dumps(list(m.chains)), m.preset,
             m.ticket_usd, m.slots, json.dumps(dict(m.breakers)), m.exit_preset,
             json.dumps(dict(m.report)), int(m.post_public), m.stage, m.actor,
             m.created_at, m.updated_at))
    except sqlite3.IntegrityError:
        raise MandateError("this user already has a live trading mandate — stop it first") \
            from None
    return m


def _require(mandate_id: str, path: Optional[str]) -> Mandate:
    m = get(mandate_id, path=path)
    if m is None:
        raise MandateError(f"no trading mandate {mandate_id!r}")
    return m


def _update(mandate_id: str, path: Optional[str], now: Optional[float], **cols) -> Mandate:
    from core.sqlite_util import execute_retry
    p = path or db_path()
    cols["updated_at"] = time.time() if now is None else float(now)
    sets = ", ".join(f"{k}=?" for k in cols)
    execute_retry(p, f"UPDATE trading_mandates SET {sets} WHERE id=?",
                  tuple(cols.values()) + (str(mandate_id),))
    return _require(mandate_id, path)


def set_status(mandate_id: str, status: str, *, actor: str, path: Optional[str] = None,
               now: Optional[float] = None) -> Mandate:
    """``active`` | ``paused`` | ``stopped``. Pausing tightens (any actor); resuming
    and stopping are the owner's. ``halted`` is :func:`latch`; leaving it is
    :func:`release`. ``stopped`` is final."""
    if status not in (ACTIVE, PAUSED, STOPPED):
        raise MandateError(f"set_status takes active|paused|stopped (got {status!r}); "
                           f"a halt is latch(), its release is release()")
    m = _require(mandate_id, path)
    if m.status == STOPPED:
        raise MandateError("a stopped mandate is final — start a new one")
    if status == m.status:
        return m
    if status in (ACTIVE, STOPPED) and actor != OWNER_SEAT:
        raise MandateError(f"only the owner may set a mandate {status}")
    if m.status == HALTED and status != STOPPED:
        raise MandateError("a halted mandate is released with release(), by the owner "
                           "(or stopped)")
    extra = {"stopped_by": actor} if status == STOPPED else {}
    return _update(mandate_id, path, now, status=status, **extra)


def latch(mandate_id: str, reason: str, *, path: Optional[str] = None,
          now: Optional[float] = None) -> Mandate:
    """Halt entries (a breaker). Persists across restarts; the FIRST reason stands
    until the owner releases. A stopped mandate is not latched."""
    m = _require(mandate_id, path)
    if m.status in (HALTED, STOPPED):
        return m
    ts = time.time() if now is None else float(now)
    return _update(mandate_id, path, now, status=HALTED, halted_reason=str(reason),
                   halted_at=ts)


def release(mandate_id: str, *, actor: str, path: Optional[str] = None,
            now: Optional[float] = None) -> Mandate:
    """Clear a latched breaker. Owner only. ``released_at`` is the breakers' new
    baseline: the P&L and LLM spend before it cannot trip them again."""
    if actor != OWNER_SEAT:
        raise MandateError("only the owner releases a latched breaker")
    m = _require(mandate_id, path)
    if m.status != HALTED:
        raise MandateError(f"mandate {mandate_id} is {m.status}, not halted")
    ts = time.time() if now is None else float(now)
    return _update(mandate_id, path, now, status=ACTIVE, halted_reason=None, halted_at=None,
                   released_at=ts)


def set_cooldown(mandate_id: str, hours: float, *, path: Optional[str] = None,
                 now: Optional[float] = None) -> Mandate:
    """The consecutive-losses breaker: no entries for *hours*; it expires by itself."""
    ts = time.time() if now is None else float(now)
    return _update(mandate_id, path, now, cooldown_at=ts,
                   cooldown_until=ts + float(hours) * 3600.0)


def set_stage(mandate_id: str, stage: str, *, path: Optional[str] = None,
              now: Optional[float] = None) -> Mandate:
    if stage not in STAGES:
        raise MandateError(f"stage must be one of {STAGES} (got {stage!r})")
    _require(mandate_id, path)
    return _update(mandate_id, path, now, stage=stage)


def record_entry(mandate_id: str, *, chain: str, token: str, usd: Optional[float] = None,
                 path: Optional[str] = None, now: Optional[float] = None) -> None:
    """Book one FILLED entry (the daily entry count reads it). Called by the
    lifecycle on a position ``opened`` event, never by the authorizer."""
    from core.sqlite_util import execute_retry
    p = init_schema(path)
    execute_retry(p, "INSERT INTO mandate_entries (mandate_id, ts, chain, token, usd) "
                  "VALUES (?,?,?,?,?)",
                  (str(mandate_id), time.time() if now is None else float(now),
                   str(chain or "").strip().lower(), str(token or ""), usd))


def apply_breaker(mandate_id: str, name: str, *, path: Optional[str] = None,
                  now: Optional[float] = None) -> Mandate:
    """What a tripped breaker does: ``consecutive_losses`` -> a cooldown that
    expires; every other breaker -> a latch only the owner releases."""
    m = _require(mandate_id, path)
    if name == "consecutive_losses":
        hours = m.effective_preset().breakers.cooldown_h or 0.0
        return set_cooldown(mandate_id, hours, path=path, now=now)
    return latch(mandate_id, f"breaker:{name}", path=path, now=now)


def mandate_id_from_context(execution_context) -> Optional[str]:
    """The run's mandate id (``metadata["mandate"]``), or None. Never raises."""
    meta = getattr(execution_context, "metadata", None)
    if not isinstance(meta, dict):
        return None
    v = meta.get(METADATA_KEY)
    v = str(v).strip() if v is not None else ""
    return v or None


# -- the promotion ladder (086 §3.8) -------------------------------------------------

def stage_ticket(mandate: Mandate, preset: Optional[Preset] = None) -> float:
    """The max cost of one entry at the mandate's stage: probe = the probe ticket
    (never above the mandate ticket), small = the small fraction, normal = full."""
    p = preset or mandate.effective_preset()
    ladder = p.promotion
    ticket = float(mandate.ticket_usd)
    if mandate.stage == "probe":
        return min(float(ladder.probe_ticket_usd), ticket)
    if mandate.stage == "small":
        return round(ticket * float(ladder.small_fraction), 2)
    return round(ticket * float(ladder.normal_fraction), 2)


def normal_added_risk_usd(mandate: Mandate, preset: Optional[Preset] = None) -> float:
    """The added risk of ``normal`` over ``small``: the extra ticket per slot, lost
    at the stop."""
    p = preset or mandate.effective_preset()
    extra = float(mandate.ticket_usd) * (p.promotion.normal_fraction - p.promotion.small_fraction)
    stop = abs(p.exit.stop_pct) / 100.0 if p.exit else 1.0
    return round(extra * int(mandate.slots) * stop, 2)


def next_stage(mandate: Mandate, *, probe_passed_chains: Iterable[str] = (),
               small_realized_usd: float = 0.0, small_closed_trades: int = 0,
               breaker_tripped: bool = False, preset: Optional[Preset] = None) -> str:
    """The stage the mandate should be at. A breaker trip drops one stage; ``probe``
    -> ``small`` once a probe passed on EVERY mandate chain; ``small`` -> ``normal``
    once the realized P&L at small covers the added risk of normal after the
    preset's minimum number of closed trades."""
    p = preset or mandate.effective_preset()
    stage = mandate.stage if mandate.stage in STAGES else "probe"
    i = STAGES.index(stage)
    if breaker_tripped:
        return STAGES[max(0, i - 1)]
    if stage == "probe":
        passed = {str(c).strip().lower() for c in probe_passed_chains}
        return "small" if set(mandate.chains) <= passed else "probe"
    if stage == "small":
        if (int(small_closed_trades) >= int(p.promotion.normal_min_closed_trades)
                and float(small_realized_usd) >= normal_added_risk_usd(mandate, p)):
            return "normal"
        return "small"
    return "normal"


def _register_gate() -> None:
    """Phase 1 (``core.packs.loader._IN_CORE_RAILS``): hand the money kernel its
    mandate step through the hook, so ``core.money.authorize`` never imports this
    store (or the data-home resolver it needs)."""
    from core.money import hooks

    def _gate(mandate_id, **kw):
        from core.money.mandate_gate import entry_refusal
        return entry_refusal(mandate_id, **kw)

    hooks.register_mandate_gate(_gate)


_register_gate()
