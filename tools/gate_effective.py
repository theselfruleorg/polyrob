"""093 A4: the EFFECTIVE state of a flag that a tool gate names.

``preferences explain MCP_ENABLED`` said ``True`` while the tool was off (zero
servers configured) — the agent read the flag as the state and told the owner
"MCP is already on" (prod 2026-10-28). The explain line now carries the state of
the tools that flag gates, from the SAME reader the catalog uses:

- a process with a running container (the agent's ``preferences`` action, the
  chat seats): ``tools.tool_disclosure.resolve_tool_status`` — the gate, the
  constructed tool's ``availability_gap()`` (no MCP server, no exec backend);
- a process with no container (``polyrob config explain``): the registration gate
  (``core.tool_gates``) or the flag, plus the backend probe of the exec tools.

Registered with ``core.config_service`` at import (core may not import tools).
"""
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

#: Gated reasons that do not mean "off": the tool is there, behind its own grant.
_PRESENT_REASONS = frozenset({"money", "autonomous-ceiling", "leaf-blocked"})


def _container():
    try:
        from core.container import DependencyContainer
        return getattr(DependencyContainer, "_instance", None)
    except Exception:
        return None


def _first_sentence(text: str) -> str:
    text = (text or "").strip()
    return text.split(". ", 1)[0].split("\n", 1)[0].rstrip(".")


def _static_gap(tool_id: str) -> Optional[Tuple[str, str]]:
    """The gap a tool reports without an instance (the exec backend probe)."""
    from tools.code_exec import backend_probe
    if tool_id in ("shell", "process"):
        return backend_probe.shell_backend_gap()
    if tool_id == "code_execution":
        return backend_probe.code_exec_gap()
    return None


def tool_state(tool_id: str, flag: str, value: object, container=None) -> Tuple[str, str]:
    """``(state, why)`` — state ``ON`` | ``OFF``; *why* is empty for a plain ON."""
    if container is not None:
        from tools.tool_disclosure import _container_has_tool, resolve_tool_status
        if _container_has_tool(container, tool_id):
            st = resolve_tool_status(tool_id, container=container, loaded_ids=set())
            if st.status != "gated" or st.reason in _PRESENT_REASONS:
                return "ON", ""
            return "OFF", f"{st.reason}: {_first_sentence(st.remedy)}"
    from core.tool_gates import gate_for, gate_on
    from core.env import parse_bool
    registered = gate_on(tool_id) if gate_for(tool_id) is not None else parse_bool(value, False)
    if not registered:
        if container is not None:
            return "OFF", f"{flag} is off or the tool's gate is closed — not constructed here"
        return "OFF", f"{flag} is off"
    if container is not None:
        return "OFF", "the flag is on, but this process did not construct the tool"
    gap = _static_gap(tool_id)
    if gap is not None:
        return "OFF", f"{gap[0]}: {_first_sentence(gap[1])}"
    from tools.descriptors import get_tool_class
    if tool_id not in ("shell", "process", "code_execution") and \
            hasattr(get_tool_class(tool_id), "availability_gap"):
        return "ON", ("by the flag; this process runs no agent, so the tool's own check "
                      "(e.g. its servers) is not read here — the agent's tool catalog shows it")
    return "ON", ""


def flag_effective(flag: str, value: object) -> str:
    """``effective: ON`` / ``effective: OFF — <why>`` for the tools *flag* gates
    (one tool), or ``effective: shell OFF — …; process ON`` (several)."""
    from core.config_service import gate_tools_for_flag
    tool_ids = gate_tools_for_flag(flag)
    if not tool_ids:
        return ""
    container = _container()
    states = []
    for tid in tool_ids:
        try:
            states.append((tid, *tool_state(tid, flag, value, container)))
        except Exception as exc:
            logger.debug("gate_effective: state of %s failed", tid, exc_info=True)
            states.append((tid, "unknown", f"the probe failed ({type(exc).__name__})"))
    if len(states) == 1:
        _tid, state, why = states[0]
        return f"effective: {state}" + (f" — {why}" if why else "")
    return "effective: " + "; ".join(
        f"{tid} {state}" + (f" — {why}" if why else "") for tid, state, why in states)


def _register() -> None:
    from core.config_service import register_gate_effective_probe
    register_gate_effective_probe(flag_effective)


_register()
