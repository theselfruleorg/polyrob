"""The ``packs`` status section (067 P2): every installed pack, whether it is
loaded, disabled or refused — and why. A refused pack is a health item; a pack
that is not there is not an error. Each loaded pack's own status lines follow
its row (``PackSpec.status_sections``); one that fails renders its error.
"""
from core.status_snapshot import SEVERITY_WARN, STATE_DEGRADED, HealthItem, Section


def packs_section() -> Section:
    from core.packs import state
    sec = Section(name="packs")
    error = state.discovery_error()
    if error:
        sec.state = STATE_DEGRADED
        sec.reason = f"pack discovery failed: {error}"
        sec.lines.append(f"⚠ {sec.reason}")
        sec.health.append(HealthItem(key="packs_discovery", severity=SEVERITY_WARN,
                                     text=sec.reason,
                                     remedy="run `polyrob pack doctor` and read the log"))
    recs = state.records()
    sec.data = {"packs": [{"id": r.id, "version": r.version, "tier": r.tier,
                           "status": r.status, "reason": r.reason, "errors": list(r.errors),
                           "needs": [{"tool": n.tool, "missing": list(n.missing),
                                      "remedy": n.remedy(), "withheld": n.withheld}
                                     for n in r.current_needs()]}
                          for r in recs]}
    if not recs and not error:
        sec.lines.append("no packs installed" if state.phase_done("policies")
                         else "packs not discovered in this process")
        return sec
    for rec in recs:
        sec.lines.append(rec.line())
        if rec.status == state.REFUSED or rec.errors:
            sec.health.append(HealthItem(
                key=f"pack_{rec.id}", severity=SEVERITY_WARN,
                text=f"pack {rec.id}: {rec.reason or '; '.join(rec.errors)}",
                remedy=f"polyrob pack info {rec.id}"))
        if rec.status == state.LOADED:
            for part in rec.spec.status_sections:
                sec.lines.extend(f"  {part.name}: {line}" for line in _pack_lines(part))
    return sec


def _pack_lines(part) -> list:
    try:
        return [str(line) for line in (part.build() or [])]
    except Exception as exc:  # noqa: BLE001 — rendered, never omitted
        return [f"unavailable ({type(exc).__name__}: {exc})"]
