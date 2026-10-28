"""The plan on a mandate entry + the typed close reasons (086 W6, §3.7).

A run that belongs to a trading mandate (``metadata["mandate"]`` on the execution
context) must say WHY it enters before the swap quotes anything: a typed plan
``{thesis, confidence 0..1, invalidation, preset}``. The plan is stored beside
the mandate (``trading.db``, table ``position_plans``) keyed by the book row it
opened — ``(user, chain, account, address, entry_ts)`` — and the exit job's runs
read it back with :func:`plan_for` ("follow the plan; stops only tighten").

An EXIT never needs a plan (exits are always allowed, §2.4): only a swap whose
bought side becomes a position (:func:`is_entry`, the book's own classifier) does.

Close reasons (§3.3): what a sell records on ``position_realized.close_reason``.

⚠️ A READ never creates the store; an existing store that cannot be read raises
:class:`PlanUnreadable` — "no plan" and "could not read the plan" are different.

Tier-0: imports nothing above ``core``.
"""
import os
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

#: The typed close reasons (086 §3.3). ``breaker:<name>`` carries the breaker.
CLOSE_REASONS = ("stop_loss", "take_profit", "trailing", "time_stop", "owner",
                 "retired", "dust", "orphan", "manual")
BREAKER_PREFIX = "breaker:"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS position_plans (
    user_id      TEXT NOT NULL,
    chain        TEXT NOT NULL,
    account      TEXT NOT NULL DEFAULT '',
    address      TEXT NOT NULL,
    entry_ts     REAL NOT NULL,
    mandate_id   TEXT NOT NULL DEFAULT '',
    thesis       TEXT NOT NULL,
    confidence   REAL NOT NULL,
    invalidation TEXT NOT NULL,
    preset       TEXT NOT NULL,
    created_at   REAL NOT NULL,
    PRIMARY KEY (user_id, chain, account, address, entry_ts)
);
"""


class PlanUnreadable(RuntimeError):
    """The plan store exists and could not be read."""


def normalize_close_reason(value: Any) -> str:
    """A typed close reason, ``""`` for none. Raises ValueError on any other word."""
    v = str(value or "").strip().lower()
    if not v:
        return ""
    if v in CLOSE_REASONS:
        return v
    if v.startswith(BREAKER_PREFIX) and len(v) > len(BREAKER_PREFIX) \
            and len(v) <= 64 and v[len(BREAKER_PREFIX):].replace("_", "").isalnum():
        return v
    raise ValueError(f"close_reason {value!r} is not one of {', '.join(CLOSE_REASONS)} "
                     f"or breaker:<name>")


@dataclass(frozen=True)
class Plan:
    thesis: str
    confidence: float
    invalidation: str
    preset: str
    entry_ts: Optional[float] = None
    mandate_id: str = ""
    created_at: Optional[float] = None

    def render(self) -> str:
        """One line for an exit run's context."""
        return (f"plan ({self.preset}, confidence {self.confidence:.2f}): {self.thesis} "
                f"— invalidated if: {self.invalidation}. Follow the plan; stops only tighten.")


def parse_plan(plan: Any) -> Optional[Plan]:
    """A :class:`Plan` from a dict / pydantic model, ``None`` for none.
    Raises ValueError on a plan that is present and incomplete."""
    if plan is None:
        return None
    if hasattr(plan, "model_dump"):
        plan = plan.model_dump()
    if not isinstance(plan, Mapping):
        raise ValueError("plan must be an object {thesis, confidence, invalidation, preset}")
    thesis = str(plan.get("thesis") or "").strip()
    inval = str(plan.get("invalidation") or "").strip()
    preset = str(plan.get("preset") or "").strip().lower()
    try:
        conf = float(plan.get("confidence"))
    except (TypeError, ValueError):
        raise ValueError("plan.confidence must be a number from 0 to 1") from None
    missing = [k for k, v in (("thesis", thesis), ("invalidation", inval),
                              ("preset", preset)) if not v]
    if missing:
        raise ValueError(f"plan is missing {', '.join(missing)}")
    if not 0.0 <= conf <= 1.0:
        raise ValueError("plan.confidence must be a number from 0 to 1")
    return Plan(thesis=thesis[:600], confidence=conf, invalidation=inval[:400],
                preset=preset[:40])


def is_entry(chain: str, token_in: Optional[str], token_out: str, *, in_native: bool) -> bool:
    """True when this swap OPENS or adds to a position (the book's classifier):
    the bought side is a tracked position, or a major bought with a stable."""
    from core.open_positions import _asset_kind
    in_kind = _asset_kind(chain, token_in, in_native)
    out_kind = _asset_kind(chain, token_out, False)
    if in_kind == "major" and out_kind == "major":
        return False
    return out_kind == "position" or (out_kind == "major" and in_kind == "stable")


def plan_refusal(execution_context, plan: Any, *, chain: str, token_in: Optional[str],
                 token_out: str, in_native: bool) -> Optional[str]:
    """The one-line refusal for a mandate ENTRY without a valid plan, else None.
    Runs before any quote or spend. No mandate on the run = no requirement."""
    from core.trading_mandate import mandate_id_from_context
    mid = mandate_id_from_context(execution_context)
    if plan is not None:
        try:
            parse_plan(plan)
        except ValueError as exc:
            return f"refused: invalid plan — {exc}. Nothing was quoted."
    if not mid or plan is not None:
        return None
    try:
        entry = is_entry(chain, token_in, token_out, in_native=in_native)
    except Exception:
        entry = True  # cannot classify: treat as an entry (the plan is cheap)
    if not entry:
        return None
    return (f"refused: this run belongs to trading mandate {mid} — an entry needs "
            f"plan={{thesis, confidence 0..1, invalidation, preset}}. Nothing was quoted.")


# -- store --------------------------------------------------------------------------

def _path(path: Optional[str]) -> str:
    if path:
        return path
    from core.trading_mandate import db_path
    return db_path()


def _key(chain: str, account: Optional[str], address: str):
    from core.open_positions import _account_key, _norm
    return str(chain or "").strip().lower(), _account_key(account), _norm(address)


def record_plan(user_id: str, *, chain: str, address: str, entry_ts: float, plan: Any,
                account: str = "", mandate_id: str = "", path: Optional[str] = None,
                now: Optional[float] = None) -> bool:
    """Store the plan of one book row. Returns True when written. A second entry on
    the same row (same ``entry_ts``) replaces it with the newer plan."""
    uid = str(user_id or "").strip()
    p = parse_plan(plan)
    if not uid or not address or p is None:
        return False
    from core.sqlite_util import execute_retry, init_schema
    target = _path(path)
    init_schema(target, _SCHEMA, mkdir=True)
    c, a, addr = _key(chain, account, address)
    execute_retry(
        target,
        "INSERT OR REPLACE INTO position_plans (user_id, chain, account, address, entry_ts, "
        "mandate_id, thesis, confidence, invalidation, preset, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (uid, c, a, addr, float(entry_ts), str(mandate_id or ""), p.thesis, p.confidence,
         p.invalidation, p.preset, time.time() if now is None else float(now)))
    return True


def plan_for(user_id: str, chain: str, address: str, *, account: str = "",
             entry_ts: Optional[float] = None, path: Optional[str] = None) -> Optional[Plan]:
    """The plan of one book row — the one for ``entry_ts`` when given, else the
    newest. ``None`` = no plan recorded. Raises :class:`PlanUnreadable`."""
    uid = str(user_id or "").strip()
    if not uid or not address:
        return None
    target = _path(path)
    try:
        os.stat(target)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PlanUnreadable(f"plan store unreachable: {exc}") from exc
    c, a, addr = _key(chain, account, address)
    sql = ("SELECT * FROM position_plans WHERE user_id=? AND chain=? AND account=? "
           "AND address=?")
    args: tuple = (uid, c, a, addr)
    if entry_ts is not None:
        sql += " AND abs(entry_ts - ?) < 1e-6"
        args += (float(entry_ts),)
    try:
        from core.sqlite_util import execute_retry
        have = execute_retry(target, "SELECT 1 FROM sqlite_master WHERE type='table' "
                             "AND name='position_plans'", (), fetch="one")
        if not have:
            return None
        r = execute_retry(target, sql + " ORDER BY entry_ts DESC LIMIT 1", args, fetch="one")
    except Exception as exc:
        raise PlanUnreadable(f"plan store unreadable: {exc}") from exc
    if not r:
        return None
    return Plan(thesis=r["thesis"], confidence=float(r["confidence"]),
                invalidation=r["invalidation"], preset=r["preset"],
                entry_ts=float(r["entry_ts"]), mandate_id=r["mandate_id"] or "",
                created_at=float(r["created_at"]))


def record_after_entry(user_id: str, *, chain: str, address: str, plan: Any,
                       account: str = "", mandate_id: str = "",
                       book_path: Optional[str] = None,
                       path: Optional[str] = None) -> bool:
    """After a CONFIRMED entry booked its row: store the plan against that row's
    ``entry_ts``. No row (the buy did not land, or was not a position) = nothing.
    Fail-open: the trade stands whatever happens here."""
    try:
        if parse_plan(plan) is None:
            return False
        from core.open_positions import get_position
        row = get_position(user_id, chain, address, account=account, db_path=book_path)
        if row is None:
            return False
        return record_plan(user_id, chain=chain, address=address, entry_ts=row.entry_ts,
                           plan=plan, account=account, mandate_id=mandate_id, path=path)
    except Exception:
        import logging
        logging.getLogger(__name__).warning("position plan not recorded", exc_info=True)
        return False


def record_after_swap(execution_context, *, plan: Any, chain: str, address: str,
                      account: str = "", book_path: Optional[str] = None,
                      path: Optional[str] = None) -> bool:
    """:func:`record_after_entry` for a swap verb: the tenant from the execution
    context (else the ambient identity), the mandate from its metadata."""
    if plan is None:
        return False
    try:
        from core.trading_mandate import mandate_id_from_context
        uid = str(getattr(execution_context, "user_id", "") or "").strip()
        if not uid:
            from core.exec_identity import current_exec_identity
            uid = str(current_exec_identity()[0] or "").strip()
        mid = mandate_id_from_context(execution_context) or ""
    except Exception:
        return False
    return record_after_entry(uid, chain=chain, address=address, plan=plan,
                              account=account, mandate_id=mid, book_path=book_path,
                              path=path)


__all__ = ["BREAKER_PREFIX", "CLOSE_REASONS", "Plan", "PlanUnreadable", "is_entry",
           "normalize_close_reason", "parse_plan", "plan_for", "plan_refusal",
           "record_after_entry", "record_after_swap", "record_plan"]
