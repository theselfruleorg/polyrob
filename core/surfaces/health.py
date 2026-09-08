"""Surface-health view (030 WS-G3; 2026-07-13 parity handoff A1).

Nothing ever showed WHICH surfaces are connected and whether their delivery
paths are alive — the operator learned a surface was down from a missing
message. One read-only builder every seat can render: per registered surface,
its capabilities, router subscription, circuit state and dead-target count.
"""
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def surface_health(container: Any) -> List[Dict]:
    """Rows: {surface_id, registered, subscribed, media_out, streaming,
    max_message, circuit, dead_targets}. Pure read; every probe fail-open."""
    rows: List[Dict] = []
    if container is None:
        return rows
    try:
        reg = container.get_service("surface_registry")
        router = container.get_service("message_router")
        breaker = container.get_service("surface_circuit_breaker")
        dt = container.get_service("dead_targets")
    except Exception:
        return rows
    surfaces = []
    try:
        surfaces = list(reg.all()) if reg is not None else []
    except Exception:
        surfaces = []
    subscribed_ids = set()
    try:
        subscribed_ids = set(getattr(router, "_surfaces", {}) or {})
    except Exception:
        pass
    seen = set()
    for s in surfaces:
        sid = getattr(s, "surface_id", "") or ""
        seen.add(sid)
        caps = getattr(s, "capabilities", None)
        circuit = "n/a"
        try:
            if breaker is not None:
                circuit = "OPEN" if breaker.is_open(sid) else "closed"
        except Exception:
            circuit = "unknown"
        dead = 0
        try:
            if dt is not None and hasattr(dt, "count_for_surface"):
                dead = int(dt.count_for_surface(sid))
        except Exception:
            dead = 0
        rows.append({
            "surface_id": sid,
            "registered": True,
            "subscribed": sid in subscribed_ids,
            "media_out": bool(getattr(caps, "media_out", False)),
            "streaming": bool(getattr(caps, "supports_streaming", False)),
            "max_message": getattr(caps, "max_message_bytes", None),
            "circuit": circuit,
            "dead_targets": dead,
        })
    # A router subscription with no registration is exactly the silent gap
    # WS-B2 closed — surface it here too, so a regression is visible.
    for sid in sorted(subscribed_ids - seen):
        rows.append({
            "surface_id": sid, "registered": False, "subscribed": True,
            "media_out": None, "streaming": None, "max_message": None,
            "circuit": "n/a", "dead_targets": 0,
        })
    return rows


def render_surface_health(rows: List[Dict]) -> List[str]:
    """One line per surface, identical on every seat."""
    if not rows:
        return ["no surfaces registered (chat-surface bus not installed)"]
    out = []
    for r in rows:
        bits = []
        bits.append("registered" if r.get("registered") else
                    "SUBSCRIBED-BUT-UNREGISTERED (WS-B2 regression)")
        if r.get("registered"):
            bits.append("media" if r.get("media_out") else "no-media")
            bits.append(f"circuit:{r.get('circuit')}")
            if r.get("dead_targets"):
                bits.append(f"dead-targets:{r['dead_targets']}")
        out.append(f"{r['surface_id']}: " + ", ".join(bits))
    return out
