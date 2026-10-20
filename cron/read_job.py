"""Deterministic cron jobs: a READ verb with no model step, and the per-job
pause window (core handoff W9; 090 §3/§7, 080/090 D45).

Read job — ``payload.read_verb``::

    {"verb": "defi_data.pool_metrics",
     "params": {"chain": "robinhood", "token": "0x…", "expect_pool_id": "0x…"},
     "deliver_on": "alert"}          # "alert" (default) | "always" | "never"

The runner calls the verb in-process ($0 — no session, no LLM turn), records
the outcome as a ``cron_run`` event, and delivers the text through the cron
delivery rail when ``deliver_on`` says so (an ``ALERT`` line in the result is
an alert; an error is an alert). Only verbs in ``READ_VERBS`` (the ``read_job``
rows of the verb-policy table) may run: each is
a read on a tool the capability table does not mark ``money`` or
``high_impact`` (pinned by ``tests/unit/cron/test_read_job.py``).

Pause window — ``payload.pause_windows``: a list of ``[start, end]`` pairs
(epoch seconds, or ISO-8601 UTC). A tick inside a window is a $0
``cron_run skipped/pause_window`` for ANY job kind — e.g. the 6-hourly buyback
paused from T-1 h to 24 h after the mint window (090 R5, D45). A malformed
window is refused when the job is scheduled or edited, never silently ignored.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.lazy_views import lazy_module_getattr, view

logger = logging.getLogger(__name__)

#: tool id -> (module, tool class): where a read job's verb lives. The VERBS are
#: the ``read_job`` rows of the verb-policy table (W4; 071 §3.9 $0 watches —
#: PUBLIC reads only, the reason per verb beside its row).
_READ_JOB_TOOLS: Dict[str, Tuple[str, str]] = {
    "defi_data": ("tools.defi.data_tool", "DefiDataTool"),
}


def _build_read_verbs() -> Dict[str, Tuple[str, str, str, str]]:
    """verb -> (module, tool class, action, params class), from the rows."""
    import importlib
    from core.action_names import bare_action_name
    from core.verb_policy import ordered_ids_where, policy_for
    out = {}
    for name in ordered_ids_where(read_job=True):
        tool = policy_for(name).tool
        if tool not in _READ_JOB_TOOLS:   # a row with no locator here cannot run
            continue
        module, cls = _READ_JOB_TOOLS[tool]
        action = bare_action_name(tool, name)
        model = getattr(getattr(importlib.import_module(module), cls), action)._param_model
        out[f"{tool}.{action}"] = (module, cls, action, model.__name__)
    return dict(sorted(out.items()))


#: verb -> (module, tool class, action, params class): built on first read.
__getattr__ = lazy_module_getattr(__name__, {"READ_VERBS": _build_read_verbs})


def _read_verbs() -> Dict[str, Tuple[str, str, str, str]]:
    return view(__name__, "READ_VERBS")


DELIVER_ON = ("alert", "always", "never")
MAX_WINDOWS = 8


class ReadJobError(ValueError):
    """A read-job or pause-window payload that cannot run as written."""


# -- pause windows -----------------------------------------------------------

def _ts(value: Any) -> float:
    if isinstance(value, bool):
        raise ReadJobError("a pause window bound must be a time, not a bool")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReadJobError(f"pause window bound {value!r} is not epoch seconds or ISO-8601") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def normalize_pause_windows(value: Any) -> Optional[List[List[float]]]:
    """``[[start, end], …]`` in epoch seconds, or None to drop the key. Raises
    ReadJobError on a malformed or empty window."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise ReadJobError("pause_windows must be a list of [start, end] pairs")
    if value and not isinstance(value[0], (list, tuple, dict)):
        value = [value]                       # one bare [start, end] pair
    if len(value) > MAX_WINDOWS:
        raise ReadJobError(f"at most {MAX_WINDOWS} pause windows per job")
    out = []
    for w in value:
        if isinstance(w, dict):
            w = [w.get("from"), w.get("to")]
        if not isinstance(w, (list, tuple)) or len(w) != 2:
            raise ReadJobError("each pause window is [start, end]")
        start, end = _ts(w[0]), _ts(w[1])
        if not end > start:
            raise ReadJobError("a pause window must end after it starts")
        out.append([start, end])
    return out


def active_pause_window(payload: Dict[str, Any], now: Optional[float] = None) -> Optional[List[float]]:
    """The window ``now`` falls in, else None. A stored window that no longer
    parses holds the job (fail CLOSED: a pause the owner set must not lapse
    because it was written badly — schedule/edit already refuse that)."""
    raw = (payload or {}).get("pause_windows")
    if not raw:
        return None
    now = time.time() if now is None else now
    try:
        windows = normalize_pause_windows(raw) or []
    except ReadJobError:
        return [float("-inf"), float("inf")]
    for start, end in windows:
        if start <= now < end:
            return [start, end]
    return None


# -- read verbs --------------------------------------------------------------

def validate_read_verb(spec: Any) -> Dict[str, Any]:
    """The normalized spec, or ReadJobError. Validates the params against the
    verb's own param model now, so a bad job never reaches a tick."""
    if not isinstance(spec, dict):
        raise ReadJobError("read_verb must be an object {verb, params, deliver_on}")
    verb = str(spec.get("verb") or "")
    if verb not in _read_verbs():
        raise ReadJobError(f"read_verb {verb!r} is not a deterministic read verb; "
                           f"allowed: {', '.join(sorted(_read_verbs()))}")
    deliver_on = str(spec.get("deliver_on") or "alert")
    if deliver_on not in DELIVER_ON:
        raise ReadJobError(f"deliver_on must be one of {', '.join(DELIVER_ON)}")
    params = spec.get("params") or {}
    if not isinstance(params, dict):
        raise ReadJobError("read_verb.params must be an object")
    _cls, model = _load(verb)
    try:
        model(**params)
    except Exception as exc:
        raise ReadJobError(f"read_verb params do not validate: {exc}") from exc
    out = {"verb": verb, "params": params, "deliver_on": deliver_on}
    alert = normalize_alert(spec.get("alert"))
    if alert:
        out["alert"] = alert
    return out


def normalize_alert(value: Any) -> Optional[Dict[str, Any]]:
    """``{"field": "<metadata key, dot path>", "below": n, "above": n}`` or None.

    071 §3.9: a watch fires on a threshold the OWNER set, read from the verb's
    typed ``metadata`` — never from prose. At least one bound is required."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ReadJobError("read_verb.alert must be an object {field, below?, above?}")
    field = str(value.get("field") or "").strip()
    if not field:
        raise ReadJobError("read_verb.alert needs a field (a key of the verb's metadata)")
    out: Dict[str, Any] = {"field": field}
    for bound in ("below", "above"):
        if value.get(bound) is not None:
            try:
                out[bound] = float(value[bound])
            except (TypeError, ValueError) as exc:
                raise ReadJobError(f"read_verb.alert.{bound} must be a number") from exc
    if len(out) == 1:
        raise ReadJobError("read_verb.alert needs below and/or above")
    return out


def _metadata_value(metadata: Any, path: str):
    cur = metadata
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def alert_lines(alert: Optional[Dict[str, Any]], metadata: Any) -> List[str]:
    """The ALERT lines a threshold produces. An unreadable value ALERTS — a
    watch that cannot see its number must not read as "all quiet"."""
    if not alert:
        return []
    field = alert["field"]
    raw = _metadata_value(metadata, field)
    try:
        value = None if raw is None or isinstance(raw, bool) else float(raw)
    except (TypeError, ValueError):
        value = None
    if value is None:
        return [f"ALERT: {field} is UNKNOWN — the watch could not read it (not zero)."]
    out = []
    if "below" in alert and value < alert["below"]:
        out.append(f"ALERT: {field} = {value:,.6g} is below {alert['below']:,.6g}.")
    if "above" in alert and value > alert["above"]:
        out.append(f"ALERT: {field} = {value:,.6g} is above {alert['above']:,.6g}.")
    return out


def _load(verb: str):
    import importlib
    module, cls, _action, model = _read_verbs()[verb]
    mod = importlib.import_module(module)
    return getattr(mod, cls), getattr(mod, model)


async def call_read_verb(spec: Dict[str, Any], *, tool_factory=None) -> Tuple[bool, str]:
    """``(ok, text)``. Never raises; a verb error is ``(False, error)``."""
    try:
        spec = validate_read_verb(spec)
        cls, model = _load(spec["verb"])
        tool = (tool_factory or cls)()
        action = _read_verbs()[spec["verb"]][2]
        result = await getattr(tool, action)(model(**spec["params"]))
    except Exception as exc:
        return False, f"read job error: {exc}"
    err = getattr(result, "error", None)
    if err:
        return False, str(err)
    text = str(getattr(result, "extracted_content", "") or "")
    lines = alert_lines(spec.get("alert"), getattr(result, "metadata", None))
    if lines:
        text = "\n".join(lines) + ("\n\n" + text if text else "")
    return True, text


def should_deliver(deliver_on: str, ok: bool, text: str) -> bool:
    if deliver_on == "always":
        return True
    if deliver_on == "never":
        return False
    return (not ok) or any(line.startswith("ALERT") for line in text.splitlines())


async def run_read_job(task_agent: Any, job: Any, *, tool_factory=None) -> Tuple[bool, str]:
    """Run the job's read verb and deliver per ``deliver_on``. Returns
    ``(ok, outcome_reason)``; never raises."""
    payload = dict(getattr(job, "payload", None) or {})
    spec = payload.get("read_verb") or {}
    ok, text = await call_read_verb(spec, tool_factory=tool_factory)
    deliver_on = str((spec or {}).get("deliver_on") or "alert")
    if not should_deliver(deliver_on, ok, text):
        return ok, "read_verb"
    target = payload.get("deliver") or "telegram"
    try:
        from cron.delivery import deliver_result
        sent = await deliver_result(task_agent, job, text, target=target,
                                    deliver_target=payload.get("deliver_target"))
    except Exception:
        logger.warning("cron job %s: read-job delivery failed", getattr(job, "id", "?"),
                       exc_info=True)
        sent = False
    return ok, "read_verb_delivered" if sent else "read_verb_undelivered"


# -- $0 pre-check for watch jobs ---------------------------------------------
# (lives in this module, not its own, so the cron import cycle gains no member
# — tests/test_layering_ratchet.py)
"""$0 pre-check for watch jobs (2026-10-14 review A-12).

Monitor / scout / exit / watcher jobs mostly find nothing: ≈ $44 of ≈ $122 in
30 days went to LLM runs that read a price and concluded "no change" (EXIT
MONITOR: 24 runs, 0 useful). The 071 ``$0 watches`` pattern (``cron/read_job.py``)
already reads a PUBLIC verb in-process with no model turn; a pre-check reuses it
IN FRONT of an LLM job:

    payload.precheck = {
        "verb": "defi_data.price",                 # a read_verb (READ_VERBS rows)
        "params": {"chain": "base", "address": "0x…"},
        "alert": {"field": "price_usd", "below": 0.8, "above": 1.4},   # optional
        "move_pct": 5,             # optional, needs "field": run on a ≥ 5 % move
        "field": "price_usd",      # the metadata key move_pct watches
        "max_quiet_hours": 24,     # optional: run anyway after this long
    }

The verb runs first ($0). The tick is a ``cron_run skipped/precheck_quiet``
(no session, no model call) when the verb read cleanly AND nothing fired:
no ``alert`` bound crossed, and the ``field`` moved less than ``move_pct`` since
the last tick that DID run the model. When something fired, the model runs and
the pre-check's own text rides into the task as ``<precheck>`` so the run does
not read the same number again.

Fail-OPEN by construction: an unknown verb, a read error, an unreadable value,
a missing baseline or any exception RUNS the tick — a watch that cannot see its
number must never read as "all quiet" (the read-job alert rule). The baseline
(the last value that ran the model) lives in the wake-gate side table in
``cron.db`` under ``precheck:<job id>`` — no new store.
"""
from dataclasses import dataclass as _dataclass, field as _field  # noqa: E402

PAYLOAD_KEY = "precheck"
#: The ``cron_run`` reason a quiet pre-check records.
QUIET_REASON = "precheck_quiet"
_BASELINE_PREFIX = "precheck:"


class PrecheckError(ValueError):
    """A pre-check payload that cannot run as written (refused at schedule/edit)."""


@_dataclass
class PrecheckVerdict:
    #: True = a $0 tick (record ``cron_run skipped/precheck_quiet``, no model)
    skip: bool
    reason: str
    attrs: Dict[str, Any] = _field(default_factory=dict)
    #: the read's text, handed to the run as ``<precheck>`` when it runs
    context: str = ""
    #: the watched value this tick read (recorded as the baseline after a run)
    value: Optional[float] = None


def validate_precheck(spec: Any) -> Dict[str, Any]:
    """The normalized spec, or :class:`PrecheckError`. The verb, its params and
    the alert go through the read-job validator (one allowlist, one param check)."""
    if not isinstance(spec, dict):
        raise PrecheckError("precheck must be an object {verb, params, alert?, move_pct?, "
                            "field?, max_quiet_hours?}")
    try:
        base = validate_read_verb({"verb": spec.get("verb"), "params": spec.get("params") or {},
                                   "alert": spec.get("alert")})
    except ReadJobError as exc:
        raise PrecheckError(f"precheck: {exc}") from exc
    out: Dict[str, Any] = {"verb": base["verb"], "params": base["params"]}
    if base.get("alert"):
        out["alert"] = base["alert"]
    if spec.get("move_pct") is not None:
        try:
            move = float(spec["move_pct"])
        except (TypeError, ValueError) as exc:
            raise PrecheckError("precheck.move_pct must be a number (percent)") from exc
        if not move > 0:
            raise PrecheckError("precheck.move_pct must be above 0")
        fld = str(spec.get("field") or "").strip()
        if not fld:
            raise PrecheckError("precheck.move_pct needs field (the metadata key it watches)")
        out["move_pct"] = move
        out["field"] = fld
    if spec.get("max_quiet_hours") is not None:
        try:
            hours = float(spec["max_quiet_hours"])
        except (TypeError, ValueError) as exc:
            raise PrecheckError("precheck.max_quiet_hours must be a number") from exc
        if not hours > 0:
            raise PrecheckError("precheck.max_quiet_hours must be above 0")
        out["max_quiet_hours"] = hours
    if "alert" not in out and "move_pct" not in out:
        raise PrecheckError("precheck needs alert {field, below?, above?} and/or "
                            "move_pct + field — otherwise nothing can ever say 'quiet'")
    return out


def _store(data_dir: str):
    from core.runtime_paths import cron_db_path
    from cron.wake_gate import WakeGateStore
    return WakeGateStore(cron_db_path(data_dir))


def _baseline(job_id: str, data_dir: str):
    """``(value, age_seconds)`` of the last tick that ran the model, or None."""
    from core.sqlite_util import execute_retry
    st = _store(data_dir)
    row = execute_retry(st.db_path,
                        "SELECT fingerprint, updated_at FROM wake_gate WHERE job_id=?",
                        (_BASELINE_PREFIX + job_id,), fetch="one")
    if not row:
        return None
    try:
        return float(row[0]), max(0.0, time.time() - float(row[1]))
    except (TypeError, ValueError):
        return None


def record_baseline(job: Any, value: Optional[float], *, data_dir: str) -> None:
    """After a tick that RAN the model: its value is the next tick's baseline.
    A tick with no readable value leaves the old baseline. Never raises."""
    if value is None:
        return
    try:
        from core.sqlite_util import execute_retry
        st = _store(data_dir)
        execute_retry(st.db_path,
                      "INSERT INTO wake_gate (job_id, user_id, fingerprint, ok, updated_at) "
                      "VALUES (?,?,?,1,?) ON CONFLICT(job_id) DO UPDATE SET "
                      "fingerprint=excluded.fingerprint, ok=1, updated_at=excluded.updated_at",
                      (_BASELINE_PREFIX + str(getattr(job, "id", "")),
                       str(getattr(job, "user_id", "") or ""), repr(float(value)), time.time()))
    except Exception:
        logger.warning("precheck: baseline for job %s not recorded", getattr(job, "id", "?"),
                       exc_info=True)


def _value(metadata: Any, path: str) -> Optional[float]:
    raw = _metadata_value(metadata, path)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


async def run_precheck(job: Any, *, data_dir: str, tool_factory=None) -> Optional[PrecheckVerdict]:
    """None when the job declares no pre-check. Otherwise a verdict; ANY doubt
    is ``skip=False`` (run the model). Never raises."""
    payload = dict(getattr(job, "payload", None) or {})
    spec = payload.get(PAYLOAD_KEY)
    if not spec:
        return None
    try:
        spec = validate_precheck(spec)
    except PrecheckError as exc:
        logger.warning("cron job %s: %s — running the tick", getattr(job, "id", "?"), exc)
        return PrecheckVerdict(False, "precheck_invalid", {"error": str(exc)[:200]})
    try:
        cls, model = _load(spec["verb"])
        tool = (tool_factory or cls)()
        action = _read_verbs()[spec["verb"]][2]
        result = await getattr(tool, action)(model(**spec["params"]))
    except Exception as exc:
        return PrecheckVerdict(False, "precheck_error", {"error": f"{type(exc).__name__}"})
    err = getattr(result, "error", None)
    text = str(getattr(result, "extracted_content", "") or "")
    if err:
        return PrecheckVerdict(False, "precheck_error", {"error": str(err)[:200]},
                               context=f"The pre-check read FAILED: {str(err)[:300]}")
    metadata = getattr(result, "metadata", None)
    lines = alert_lines(spec.get("alert"), metadata)
    watched = spec.get("field") or (spec.get("alert") or {}).get("field")
    value = _value(metadata, watched) if watched else None
    ctx = ("\n".join(lines) + "\n\n" if lines else "") + text[:1500]
    if lines:
        return PrecheckVerdict(False, "precheck_alert", {"alerts": len(lines)}, ctx, value)
    attrs: Dict[str, Any] = {"precheck": spec["verb"]}
    if value is not None:
        attrs["value"] = value
    if "move_pct" in spec and value is None:
        return PrecheckVerdict(False, "precheck_unreadable", attrs,
                               f"The pre-check could not read {watched} — it is UNKNOWN, "
                               f"not unchanged.\n\n{text[:1500]}", None)
    base = None
    if "move_pct" in spec or spec.get("max_quiet_hours"):
        try:
            base = _baseline(str(getattr(job, "id", "")), data_dir)
        except Exception:
            base = None
        if base is None:
            # No tick has run the model yet (or the baseline is unreadable).
            return PrecheckVerdict(False, "precheck_no_baseline", attrs, ctx, value)
        if spec.get("max_quiet_hours") and base[1] >= spec["max_quiet_hours"] * 3600:
            return PrecheckVerdict(False, "precheck_quiet_too_long", attrs, ctx, value)
    if "move_pct" in spec:
        prev = base[0]
        moved = abs(value - prev) / abs(prev) * 100.0 if prev else float("inf")
        attrs.update(baseline=prev, moved_pct=round(moved, 3) if moved != float("inf") else None)
        if moved >= spec["move_pct"]:
            return PrecheckVerdict(False, "precheck_moved", attrs, ctx, value)
    return PrecheckVerdict(True, QUIET_REASON, attrs, ctx, value)


def context_block(verdict: Optional[PrecheckVerdict]) -> str:
    """The ``<precheck>`` block a run that the pre-check let through carries."""
    if verdict is None or verdict.skip or not verdict.context:
        return ""
    return (f"<precheck reason=\"{verdict.reason}\">\nThe job's $0 pre-check already read "
            f"this — use it; do not read the same figure again.\n{verdict.context}\n</precheck>")


