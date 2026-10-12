"""Status sections: ``session`` and ``providers``.

Moved out of ``core/status_snapshot.py`` (W9, 2026-10-10 structural review)
unchanged. ``core.status_snapshot`` stays the ONE assembler and re-exports
every name here; the store reads go through ``core.status_snapshot._rows``
late-bound, so a test that patches it still reaches this builder.
"""
from __future__ import annotations

from typing import Any, Optional

from core.status_types import (  # noqa: F401
    HealthItem, SEVERITY_CRIT, SEVERITY_WARN, Section, _K_CREDIT_SENTINEL, _K_CRON_RUN,
    _hhmm, _tele_rows,
)


def _session_section(task_agent: Any, session_id: Optional[str]) -> Section:
    sec = Section(name="session")
    if not session_id:
        sec.lines.append("no active session")
        sec.data["bound"] = False
        return sec
    short = session_id[:12] + ("…" if len(session_id) > 12 else "")
    sec.data.update({"bound": True, "session_id": session_id})
    get_orch = getattr(task_agent, "get_orchestrator", None)
    orch = get_orch(session_id) if callable(get_orch) else None
    if orch is None:
        sec.lines.append(f"bound ({short}) — idle (not resident)")
        sec.data["state"] = "idle"
        return sec
    # Real liveness: run_session holds the per-session execution lock for the
    # whole run, so `locked()` means "a step is executing right now". Queued
    # input (the old signal) is a different fact and is reported as such.
    locks = getattr(task_agent, "_session_execution_locks", None)
    lock = locks.get(session_id) if isinstance(locks, dict) else None
    has_pending = getattr(task_agent, "_session_has_pending_input", None)
    pending = bool(has_pending(session_id)) if callable(has_pending) else False
    if lock is not None:
        state = "running" if lock.locked() else ("queued input" if pending else "idle")
    else:
        state = "state unknown (no execution lock)" if not pending else "queued input"
    extra = []
    agent_obj = next(iter((getattr(orch, "agents", None) or {}).values()), None)
    if agent_obj is not None:
        st = getattr(agent_obj, "state", None)
        n_steps = getattr(st, "n_steps", None)
        if state == "running" and n_steps is not None:
            state = f"running (step {n_steps})"
        model = getattr(agent_obj, "model_name", None)
        if model:
            extra.append(f"model={model}")
        mm = getattr(agent_obj, "message_manager", None)
        if mm is not None and hasattr(mm, "get_context_usage_percent"):
            pct = mm.get_context_usage_percent()
            if pct is not None:
                extra.append(f"ctx={pct:.0f}%")
    sec.data["state"] = state
    sec.lines.append(f"bound ({short}) — {state}" + (" " + " ".join(extra) if extra else ""))
    return sec


def _providers_section(tele: Section, now: float) -> Section:
    sec = Section(name="providers")
    from core.credit_sentinel import credit_sentinel_enabled, credit_sentinel_status
    latched = credit_sentinel_status()  # raises => whole section unavailable (honest)
    sec.data["sentinel"] = latched
    sec.data["sentinel_enabled"] = credit_sentinel_enabled()
    from core.runtime_config import (operator_provider_pin, resolve_live_provider,
                                     usable_providers_with_credentials)
    try:
        usable = list(usable_providers_with_credentials())
    except Exception:
        usable = []
    pin = operator_provider_pin()
    live = resolve_live_provider(pin)
    sec.data.update({"usable": usable, "pin": pin, "live": live})
    for name, entry in sorted(latched.items()):
        who = "ALL providers" if name == "*" else name
        blocks_live_provider = name == "*" or name == live
        release = entry.get("release_ts")
        until = (f"until ~{_hhmm(release)}" if release
                 else f"auto-release {_hhmm(float(entry.get('ts') or 0) + _release_window())}")
        reason = (entry.get("reason") or "").strip().replace("\n", " ")[:140]
        serving_note = ""
        if not blocks_live_provider and live:
            serving_note = f"; does NOT block live provider {live}"
        sec.health.append(HealthItem(
            key=f"credit_sentinel:{name}",
            severity=SEVERITY_CRIT if blocks_live_provider else SEVERITY_WARN,
            text=f"credit sentinel TRIPPED for {who} since {_hhmm(entry.get('ts'))} "
                 f"({until})" + (f": {reason}" if reason else "") + serving_note,
            remedy="top up the account, or remove <data>/CREDIT_SENTINEL after topping up"))
    if live is None and usable:
        sec.health.append(HealthItem(
            key="no_live_provider", severity=SEVERITY_CRIT,
            text="NO configured provider can serve (all credit-dead) — every paid tick is skipped",
            remedy="add credits or connect another provider (`polyrob auth add`)"))
    line = f"live provider: {live or 'NONE'}"
    if pin:
        line += f" (pin {pin}{' — dead, rerouted' if pin != live else ''})"
    line += f"; credentialed: {', '.join(usable) if usable else 'none detected'}"
    if not sec.data["sentinel_enabled"]:
        line += "; credit sentinel DISABLED"
    sec.lines.append(line)
    if tele.available:
        trips = len(_tele_rows(tele, _K_CREDIT_SENTINEL))
        cron = _tele_rows(tele, _K_CRON_RUN)
        rerouted = sum(1 for r in cron if (r["attrs"] or {}).get("outcome") == "provider_rerouted")
        skipped = sum(1 for r in cron if (r["attrs"] or {}).get("outcome") == "skipped"
                      and (r["attrs"] or {}).get("reason") == "credit_sentinel")
        sec.data.update({"trips_window": trips, "rerouted_window": rerouted,
                         "sentinel_skips_window": skipped})
        bits = [f"{trips} sentinel trip(s)", f"{rerouted} cron run(s) rerouted off the pin"]
        if skipped:
            bits.append(f"{skipped} tick(s) skipped as credit-dead")
        sec.lines.append("24h: " + ", ".join(bits))
        if rerouted and not latched:
            sec.health.append(HealthItem(
                key="provider_rerouted", severity=SEVERITY_WARN,
                text=f"{rerouted} cron run(s) in 24h had to reroute off the pinned provider",
                remedy="check the pinned provider's credits / DEFAULT_PROVIDER"))
    else:
        sec.lines.append(f"24h trip/reroute counts unavailable ({tele.reason})")
    return sec


def _release_window() -> float:
    try:
        from core.credit_sentinel import _release_hours
        return float(_release_hours()) * 3600
    except Exception:
        return 6 * 3600
