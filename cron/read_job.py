"""Deterministic cron jobs: a READ verb with no model step, and the per-job
pause window (core handoff W9; 090 §3/§7, 080/090 D45).

Read job — ``payload.read_verb``::

    {"verb": "defi_data.pool_metrics",
     "params": {"chain": "robinhood", "token": "0x…", "expect_pool_id": "0x…"},
     "deliver_on": "alert"}          # "alert" (default) | "always" | "never"

The runner calls the verb in-process ($0 — no session, no LLM turn), records
the outcome as a ``cron_run`` event, and delivers the text through the cron
delivery rail when ``deliver_on`` says so (an ``ALERT`` line in the result is
an alert; an error is an alert). Only verbs in ``READ_VERBS`` may run: each is
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

logger = logging.getLogger(__name__)

#: verb -> (module, tool class, action, params class). Reads only.
READ_VERBS: Dict[str, Tuple[str, str, str, str]] = {
    "defi_data.pool_metrics": ("tools.defi.data_tool", "DefiDataTool", "pool_metrics",
                               "PoolMetricsParams"),
    # 071 §3.9: $0 watches. PUBLIC reads only. The own-wallet reads (portfolio,
    # positions, reconcile) are deliberately NOT listed: a read job runs with no
    # turn context, so the owner gate cannot tell who scheduled it. The result
    # is delivered to the job's own target (the owner's chat by default).
    "defi_data.price": ("tools.defi.data_tool", "DefiDataTool", "price", "TokenRefParams"),
    "defi_data.token_info": ("tools.defi.data_tool", "DefiDataTool", "token_info",
                             "TokenRefParams"),
    # An EXPLICIT address is public chain data (WalletHoldingsParams.address is
    # required, so it never defaults to the operator wallet), and only the owner
    # or the agent can schedule a job (cronjob is correspondent- and room-blocked).
    "defi_data.wallet_holdings": ("tools.defi.data_tool", "DefiDataTool", "wallet_holdings",
                                  "WalletHoldingsParams"),
}
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
    if verb not in READ_VERBS:
        raise ReadJobError(f"read_verb {verb!r} is not a deterministic read verb; "
                           f"allowed: {', '.join(sorted(READ_VERBS))}")
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
    module, cls, _action, model = READ_VERBS[verb]
    mod = importlib.import_module(module)
    return getattr(mod, cls), getattr(mod, model)


async def call_read_verb(spec: Dict[str, Any], *, tool_factory=None) -> Tuple[bool, str]:
    """``(ok, text)``. Never raises; a verb error is ``(False, error)``."""
    try:
        spec = validate_read_verb(spec)
        cls, model = _load(spec["verb"])
        tool = (tool_factory or cls)()
        action = READ_VERBS[spec["verb"]][2]
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
