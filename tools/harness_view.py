"""093 C1 — the agent's view of its own harness, per tool.

``agent_status(tool="<id>")`` renders :func:`tool_harness_view`; the plain
``agent_status`` adds :func:`gated_tools_block`. Both are READS over the ONE
status resolver (``tools.tool_disclosure.resolve_tool_status``), the ONE remedy
text home (``core.tool_gates.REMEDIES``) and the ONE who/when table next to it
(``core.tool_gates.REMEDY_ACTORS``). Nothing here decides a status: a view
that disagreed with the catalog would be the 2026-10-28 incident again (two
readers, two answers).
"""
import logging
from typing import Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _session_facts(controller) -> Tuple[object, set, bool, Optional[set]]:
    from tools.tool_disclosure import disclosure_ceiling
    container = getattr(controller, "container", None)
    try:
        loaded = set(controller.list_tools())
    except Exception:
        loaded = set()
    is_leaf = bool(getattr(controller, "_is_sub_agent", False))
    return container, loaded, is_leaf, disclosure_ceiling(controller)


def status_of(controller, tool_id: str, *, is_leaf: Optional[bool] = None):
    """The resolver's :class:`ToolStatus` for *tool_id* in this session."""
    from tools.tool_disclosure import resolve_tool_status
    container, loaded, leaf, allowed = _session_facts(controller)
    return resolve_tool_status(tool_id, container=container, loaded_ids=loaded,
                               is_leaf=leaf if is_leaf is None else is_leaf,
                               allowed_ids=allowed)


def who_when(st) -> Tuple[str, str, str]:
    """``(who, when, verb)`` for one status; loaded = nothing to do."""
    from core.tool_gates import REMEDY_ACTORS, remedy_actor
    if st.status == "loaded":
        return "-", "now", ""
    if st.status == "loadable":
        a = REMEDY_ACTORS["load_tool"]
        return a.who, a.when, f'load_tool("{st.tool_id}")'
    a = remedy_actor(st.reason, st.remedy)
    if a is None:
        return "unknown", "unknown", ""
    return a.who, a.when, a.verb


def _status_word(st) -> str:
    return st.status if st.status != "gated" else f"gated:{st.reason}"


def tool_harness_view(controller, tool_id: str, *, is_leaf: Optional[bool] = None,
                      may_propose: bool = False) -> str:
    """The five facts for ONE tool: status, the remedy text, who can act, when
    it applies, and the last recorded load failure (if any). The one-tap fix
    (``propose_setting``) is suggested only where it can run: ``may_propose`` =
    the caller's ``setting_actions.owner_turn_refusal`` is None (D4: a live owner
    turn), and never for a money tool (a money setting is never a card)."""
    st = status_of(controller, tool_id, is_leaf=is_leaf)
    who, when, verb = who_when(st)
    lines = [f"tool: {st.tool_id}", f"status: {_status_word(st)}"]
    if st.status != "loaded" and st.remedy:
        lines.append(f"remedy: {st.remedy}")
    if st.status != "loaded":
        lines.append(f"who: {who}" + (f" ({verb})" if verb else ""))
        lines.append(f"when: {when}")
    try:
        why = (controller.get_tool_load_failures() or {}).get(st.tool_id, "")
    except Exception:
        why = ""
    if why:
        lines.append(f"last load attempt: {why}")
    from core.tool_capabilities import TOOL_CAPABILITIES
    money = st.reason == "money" or "money" in TOOL_CAPABILITIES.get(st.tool_id, ())
    suggest = (st.status == "gated" and not money
               and may_propose)
    if suggest and who == "owner-verb" and verb.startswith("/"):
        lines.append("owner can fix it: propose_setting(command=\"" + verb
                     + "\" with the real values, why=...) puts a one-tap card to the owner")
    elif suggest and who == "operator-env":
        lines.append("only the operator can fix it: propose_setting(command=\"/config set "
                     "<KEY> <value>\") files it for the operator; never advise a restart "
                     "unless the remedy says so")
    return "\n".join(lines)


def _catalog_ids() -> List[str]:
    from tools.descriptors import get_tool_display_name, get_tool_init_order
    out: List[str] = []
    for name in get_tool_init_order():
        d = get_tool_display_name(name)
        if d not in out:
            out.append(d)
    try:
        from core.tool_capabilities import gate_disclosure_map
        out += sorted(set(gate_disclosure_map()) - set(out))
    except Exception:
        pass
    return out


def gated_tools_block(controller, ids: Optional[Iterable[str]] = None) -> str:
    """One compact line per GATED tool (reason · who · when); money tools are
    one line. Empty string when nothing is gated."""
    lines: List[str] = []
    money: List[str] = []
    for tid in (list(ids) if ids is not None else _catalog_ids()):
        try:
            st = status_of(controller, tid)
        except Exception as e:  # one tool, never the block
            lines.append(f"- {tid}: unavailable ({type(e).__name__})")
            continue
        if st.status != "gated":
            continue
        if st.reason == "money":
            money.append(st.tool_id)
            continue
        who, when, _verb = who_when(st)
        lines.append(f"- {st.tool_id}: gated:{st.reason} · who={who} · when={when}")
    if money:
        from core.tool_gates import REMEDY_ACTORS
        a = REMEDY_ACTORS["money"]
        lines.append(f"- money tools ({', '.join(money)}): gated:money · who={a.who} · "
                     f"when={a.when}")
    if not lines:
        return ""
    return ("harness (gated tools; agent_status(tool=\"<id>\") for one tool's remedy):\n"
            + "\n".join(lines))


__all__ = ["gated_tools_block", "status_of", "tool_harness_view", "who_when"]
