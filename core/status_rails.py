"""Status helpers for the external rails: the durable rail verdicts, the
per-rail cost bits, the pause-effect scan and the rail health items the
``loops`` section renders.

Moved out of ``core/status_snapshot.py`` (W9, 2026-10-10 structural review)
unchanged. ``core.status_snapshot`` stays the ONE assembler and re-exports
every name here; the store reads go through ``core.status_snapshot._rows``
late-bound, so a test that patches it still reaches this builder.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.status_types import (  # noqa: F401
    HealthItem, SEVERITY_CRIT, SEVERITY_WARN, Section, _K_RAIL_PRECONDITION_FAILED,
    _hhmm, _tele_rows,
)
from core.status_identity import _x_oauth2_relogin_text


def _rail_verdicts():
    """Durable rail verdicts (smtp / twitter_api / missing_key), aggregated by
    kind: an account out of X credit is ONE outage, not one per endpoint."""
    from core.credential_verdicts import active
    by_kind: Dict[str, Any] = {}
    for v in active():
        cur = by_kind.get(v.kind)
        if cur is None:
            by_kind[v.kind] = v
        else:
            # earliest first_seen, latest last_seen, summed count
            by_kind[v.kind] = type(v)(
                kind=v.kind, key=cur.key, first_seen=min(cur.first_seen, v.first_seen),
                last_seen=max(cur.last_seen, v.last_seen), count=cur.count + v.count,
                code=cur.code or v.code, remedy=cur.remedy or v.remedy,
                ttl_sec=cur.ttl_sec or v.ttl_sec)
    return sorted(by_kind.values(), key=lambda v: v.first_seen)


def _email_tool_drop_note() -> str:
    """Whether a standing SMTP 535 actually removes `email` from the autonomous
    toolset right now — the condition, never an assertion.

    ⚠️ Layering: `agents/task/constants.py::effective_autonomous_tools` OWNS
    this rule and core may not import it (`tests/test_layering_ratchet.py`
    forbids every `core/ -> agents.task.constants` edge). The two inputs it
    reads are both core-readable, so the CONDITION is re-derived from the same
    sources and pinned against the owner by
    `tests/unit/core/test_status_snapshot.py`.
    """
    from core.env import bool_env
    try:
        from core.config_policy.capability_toggles import email_provider
        provider = email_provider()
    except Exception:
        return ""  # cannot tell => claim nothing
    if provider != "smtp":
        return " — the agent's mail rides AgentMail, so sending is unaffected"
    if bool_env("STABLE_AUTONOMOUS_TOOLSET", True):  # default mirrors agents/task/constants.py
        return (" — autonomous runs still load the email tool (the toolset is "
                "frozen); every send refuses with this reason")
    return " — autonomous runs no longer request the email tool"


def _rail_verdict_line(v):
    """One line per standing verdict with its SINCE clock. Returns
    (text, severity-or-None, health key); a missing optional key is INFO (a
    choice, not a fault) and raises no health item."""
    from core.credential_verdicts import since_text
    since = since_text(v)
    n = int(v.count or 0)
    if v.kind == "smtp":
        # D69: the old text asserted "autonomous runs no longer request the
        # email tool" unconditionally. That drop is conditional
        # (`agents/task/constants.py::effective_autonomous_tools`): it does not
        # happen under an AgentMail transport, and it does not happen at all
        # when the requested toolset is frozen. Asserting it either way is a
        # confident claim about a thing this module cannot see, so the line now
        # states the CONDITION it can.
        return (f"email: SMTP login rejected (535) since {since}, {n} rejection(s)"
                f"{_email_tool_drop_note()}",
                SEVERITY_WARN, "email_auth_rejected")
    if v.kind == "imap":
        # D28: the RECEIVE half. Before this an inbound outage was rendered
        # nowhere at all — the mailbox simply went quiet and "no new mail" was
        # indistinguishable from "I cannot read the mailbox".
        return (f"email: INBOUND mailbox unreadable since {since}, {n} failure(s) — "
                f"new mail is NOT being received; "
                f"{v.remedy or 're-check the mailbox credential'}",
                SEVERITY_WARN, "email_inbound_rejected")
    if v.kind == "twitter_api":
        return (f"x api: {v.code or '402'} since {since}, {n} call(s) refused — "
                f"browser rail (x_browser) needs no API credits",
                SEVERITY_WARN, "x_api_rejected")
    if v.kind == "x_oauth2":
        return (_x_oauth2_relogin_text(v), SEVERITY_WARN, "x_oauth2_relogin")
    if v.kind == "missing_key":
        return (f"{v.key}: no API key since {since} (optional tool, not loaded)", None,
                f"missing_key:{v.key}")
    return (f"{v.kind} {v.key}: {v.code or 'rejected'} since {since}, {n}×",
            SEVERITY_WARN, f"rail_{v.kind}")


def _rail_cost_bits(slot: Optional[Dict[str, Any]]) -> List[str]:
    """``["$0.15 over 3 runs today"]`` — or nothing when the rail did not run.

    Any run whose cost was not recorded is named, never averaged away: the
    figure a reader acts on has to say how much of the window it covers.
    """
    if not slot or not slot.get("runs"):
        return []
    runs = int(slot["runs"])
    plural = "s" if runs != 1 else ""
    usd = float(slot.get("usd") or 0.0)
    unpriced = int(slot.get("unpriced") or 0)
    if usd <= 0 and unpriced >= runs:
        # NOTHING was priced. "$0.00" here would be a confident zero for a rail
        # that may well have spent money — the exact failure `$0.00` is banned
        # for elsewhere in this file. Say what is true: the cost is unknown.
        return [f"cost not recorded for {runs} run{plural} today"]
    bits = [f"${usd:.2f}" if usd > 0 else "$0"]
    bits[0] += f" over {runs} run{plural} today"
    if unpriced:
        bits.append(f"{unpriced} unpriced")
    return bits


def _effect_violations(user_id: str, data_dir: Optional[str], st: Any) -> List[str]:
    """033: every AUTONOMOUS outward act (``external_write``) that went out after
    the pause — by effect class, whichever tool, surface or MCP server made it,
    so a new writer can never walk past a closed kind list. Only a write the
    pause COVERS counts (a ``/pause social`` does not make a swap a violation),
    and a refused write (``outcome=denied``) is the pause working, not a breach.
    An unreadable store is named, never read as "no violation"."""
    from core.autonomy_control import KIND_SCOPES
    from core.effects import GATED_EFFECTS, outward_counts, pause_kind_for
    counts = outward_counts(user_id, float(st.since), data_dir=data_dir,
                            autonomous_only=True, outcomes=("ok", "error"))
    if counts is None:
        return ["outward writes unreadable (effect record)"]
    out = []
    for eff in sorted(counts):
        kind = pause_kind_for(eff) if eff in GATED_EFFECTS else None
        if kind is None:
            continue  # observe-only classes are not gated, so not a breach
        if not any(sc in KIND_SCOPES.get(kind, ()) for sc in st.scopes):
            continue
        out.append(f"{eff} write ×{counts[eff]}")
    return out


def _distinct_runs(rows: List[Dict[str, Any]]) -> int:
    """Count RUNS, not telemetry rows.

    `started` and `done` are two rows for one run: on 2026-09-23 a single X post
    was reported to the owner as "cron_run ×2" during a pause it had not even
    breached. A row carrying no id counts on its own — unknown identity means
    "count it", never "merge it", because merging would hide real activity
    behind one tally.
    """
    ids, anon = set(), 0
    for r in rows:
        attrs = r.get("attrs") or {}
        rid = attrs.get("job_id") or attrs.get("goal_id") or attrs.get("run_id")
        if rid:
            ids.add(str(rid))
        else:
            anon += 1
    return len(ids) + anon


def _external_rail_health(sec: Section, tele: Section) -> None:
    """056 WS9: three consecutive EMPTY answers from the anysite user-search
    endpoint (the 2026-09-18 19:48–22:30Z degradation shape) → WARN. Reads the
    `rail_probe` events the tool records; no network."""
    if not tele.available:
        return
    rows = [r for r in _tele_rows(tele, "rail_probe")
            if (r.get("attrs") or {}).get("rail") == "anysite:/api/twitter/search/users"]
    if len(rows) < 3:
        return
    last3 = [(r.get("attrs") or {}).get("outcome") for r in rows[:3]]  # newest-first
    sec.data["anysite_user_search_last3"] = last3
    if all(o == "empty" for o in last3):
        sec.health.append(HealthItem(
            key="anysite_search_empty", severity=SEVERITY_WARN,
            text=f"anysite user-search answered EMPTY on its last 3 calls (latest {_hhmm(rows[0].get('ts'))}) "
                 f"— the discovery rail is degraded, not the queries",
            remedy="pause collection rounds until a control query returns rows; "
                   "the search/posts and /user endpoints were unaffected last time"))


def _rail_gate_health(sec: Section, tele: Section) -> None:
    """2026-09-21: a money rail's PRECONDITION verb (`defi_data.reconcile`, the
    rails' step-1 "ledger vs chain — if a real row disagrees, STOP") refused
    every call for 9 h (ledger > 1 MB) while six rails traded without it and
    every seat read healthy. A refusal is a `rail_precondition_failed` event;
    ONE such event in the window is a CRIT — a rail running with its gate down
    must never read healthy, and the count + first time + reason say how long."""
    if not tele.available:
        return
    rows = _tele_rows(tele, _K_RAIL_PRECONDITION_FAILED)  # newest-first
    if not rows:
        return
    by_tool: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by_tool.setdefault(str((r.get("attrs") or {}).get("tool") or "?"), []).append(r)
    sec.data["rail_gate_refused"] = {k: len(v) for k, v in by_tool.items()}
    parts = []
    for tool, trs in by_tool.items():
        first, last = trs[-1], trs[0]
        reason = str((last.get("attrs") or {}).get("reason") or "").strip()[:90]
        parts.append(f"{tool} refused {len(trs)}× since {_hhmm(first.get('ts'))} "
                     f"(latest {_hhmm(last.get('ts'))}: {reason})")
    sec.health.append(HealthItem(
        key="rail_gate_refused", severity=SEVERITY_CRIT,
        text="money-rail gate down — " + "; ".join(parts[:3])
             + " — rails are running without their step-1 check",
        remedy="fix the named cause (a ledger over 1 MB: move its run logs to the "
               "runlog archive as the service user; a bad path: repoint the job); the "
               "rails' RECONCILE GATE rule skips every spend/sell step while it refuses"))
