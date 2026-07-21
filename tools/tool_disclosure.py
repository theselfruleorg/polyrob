"""Dynamic tool rig — progressive tool disclosure (S1+S2, owner directive 2026-07-19).

Mirrors the skills S-1 pattern (compact ``<skill-catalog>`` + ``load_skill``): every
session gets a compact ``<tool-catalog>`` foundation block disclosing ALL known tools
with an HONEST status, and the ``load_tool`` action materializes a ``loadable`` tool
mid-session through the existing ``Controller.load_tools_from_container`` seam — the
same path session creation uses, so this is a registry-view include, never new
construction machinery.

Statuses (rendered per tool, resolved by :func:`resolve_tool_status`):

- ``loaded``   — actions already registered in this session's Controller.
- ``loadable`` — the container can serve it; ``load_tool("<id>")`` materializes it.
- ``gated:<reason>`` — with a remedy channel, so degradation is never silent:
  ``money`` (explicit owner/goal grant only — NEVER loadable via load_tool),
  ``leaf-blocked`` (delegated child; parent must run it),
  ``unavailable-on-this-deploy`` (container never constructed it; remedy names the
  missing config or the deploy shape), ``unknown-tool``.

Hard lines: this module CONSULTS the capability SSOT (core/tool_capabilities.py),
never bypasses it; the delegation blocklist honours the DELEGATE_BLOCKED_TOOLS env
override via ``get_blocked_child_tools``; correspondent-taint, posture and approval
gates are EXECUTION-time gates and apply unchanged after a load — loading only
registers schemas, it grants no execution rights. Gated by
TOOL_PROGRESSIVE_DISCLOSURE (default OFF; ON under POLYROB_LOCAL).
"""
import logging
from dataclasses import dataclass
from typing import Iterable, Optional, Set

logger = logging.getLogger(__name__)

_ASK_OWNER = "message the owner / file an ops ask"


@dataclass(frozen=True)
class ToolStatus:
    """Resolved status for one display tool id."""
    tool_id: str
    status: str          # "loaded" | "loadable" | "gated"
    reason: str = ""     # gated only: money | leaf-blocked | unavailable-on-this-deploy | unknown-tool
    remedy: str = ""     # the channel that unblocks it — never empty for gated/loadable


def _container_has_tool(container, display_id: str) -> bool:
    """Whether the container can serve *display_id* — the same three probes
    ``load_tools_from_container`` uses (name, ``{name}_tool``, browser via
    browser_manager)."""
    if container is None:
        return False
    try:
        if container.has_service(display_id) or container.has_service(f"{display_id}_tool"):
            return True
        if display_id == "browser" and container.has_service("browser_manager"):
            return True
    except Exception:
        return False
    return False


def resolve_tool_status(
    tool_id: str,
    *,
    container,
    loaded_ids: Set[str],
    is_leaf: bool = False,
) -> ToolStatus:
    """Resolve the honest status of one tool id for this session.

    Order matters: an explicitly-granted (already loaded) tool reports ``loaded``
    even if it is a money tool — the grant happened at creation through the
    explicit channel; the money gate here only closes the *self-serve* load path.
    """
    from tools.descriptors import TOOL_DESCRIPTORS, get_tool_display_name
    from core.tool_capabilities import TOOL_CAPABILITIES

    display = get_tool_display_name((tool_id or "").strip())

    if display in loaded_ids:
        return ToolStatus(display, "loaded")

    caps = TOOL_CAPABILITIES.get(display)
    if caps is None and display not in TOOL_DESCRIPTORS:
        return ToolStatus(
            display, "gated", "unknown-tool",
            "not a known tool id — see <tool-catalog> for the valid ids")
    caps = caps or frozenset()

    if "money" in caps:
        return ToolStatus(
            display, "gated", "money",
            "money tools are explicit-grant-only — request it on the goal "
            "payload or ask the owner to grant it at session creation")

    if is_leaf:
        from tools.controller.delegation import get_blocked_child_tools
        if display in get_blocked_child_tools():
            return ToolStatus(
                display, "gated", "leaf-blocked",
                "not available to delegated sub-agents — report back and let "
                "the parent agent run this itself")

    if _container_has_tool(container, display):
        return ToolStatus(
            display, "loadable", "", f'call load_tool("{display}") to use it')

    desc = TOOL_DESCRIPTORS.get(display) or TOOL_DESCRIPTORS.get(tool_id)
    required = list(getattr(desc, "required_config", None) or [])
    if required:
        remedy = f"needs config: {', '.join(required)} — {_ASK_OWNER}"
    else:
        remedy = f"not constructed in this deploy shape — {_ASK_OWNER}"
    return ToolStatus(display, "gated", "unavailable-on-this-deploy", remedy)


def _one_liner(description: str, cap: int = 100) -> str:
    """First sentence of a descriptor description, capped for a compact catalog."""
    text = (description or "").strip().split("\n", 1)[0]
    first = text.split(". ", 1)[0].rstrip(".")
    return first[:cap]


def _status_suffix(st: ToolStatus) -> str:
    if st.status == "loaded":
        return "[loaded]"
    if st.status == "loadable":
        return f'[loadable — load_tool("{st.tool_id}")]'
    return f"[gated:{st.reason} — {st.remedy}]"


def render_tool_catalog(
    *,
    container,
    loaded_ids: Iterable[str],
    is_leaf: bool = False,
) -> str:
    """Render the compact ``<tool-catalog>`` block — one line per display tool.

    Pure render over the existing SSOTs; iterates the descriptor init order and
    dedupes runtime aliases (``browser_manager`` renders once, as ``browser``).
    """
    from tools.descriptors import (
        TOOL_DESCRIPTORS, get_tool_init_order, get_tool_display_name)

    loaded = set(loaded_ids or ())
    lines = [
        "<tool-catalog>",
        "Every tool this deployment knows about, with its HONEST status for this "
        "session. A [loadable] tool is one load_tool(\"<id>\") call away — its "
        "actions appear on the next step. A [gated:...] tool names the reason and "
        "the remedy channel; ask/act on it instead of working around a missing "
        "tool in silence.",
    ]
    seen = set()
    for name in get_tool_init_order():
        display = get_tool_display_name(name)
        if display in seen:
            continue
        seen.add(display)
        st = resolve_tool_status(
            display, container=container, loaded_ids=loaded, is_leaf=is_leaf)
        desc = _one_liner(TOOL_DESCRIPTORS[name].description)
        lines.append(f"- {display}: {desc} {_status_suffix(st)}")
    lines.append("</tool-catalog>")
    return "\n".join(lines)


async def perform_load_tool(controller, tool_id: str, execution_context=None):
    """Decision + load for the ``load_tool`` action (the closure stays thin —
    mirrors ``perform_message_send``).

    Refusals are STRUCTURED results (reason + remedy), not errors — a policy
    refusal is an answer, not a retryable failure. Only a load that *should*
    have worked and didn't returns an error result.
    """
    from tools.controller.types import ActionResult
    from tools.descriptors import get_tool_display_name

    display = get_tool_display_name((tool_id or "").strip())
    is_leaf = bool(
        getattr(execution_context, "is_sub_agent", False)
        or getattr(execution_context, "role", None) == "leaf"
        or getattr(controller, "_is_sub_agent", False))

    st = resolve_tool_status(
        display,
        container=getattr(controller, "container", None),
        loaded_ids=set(controller.list_tools()),
        is_leaf=is_leaf)

    if st.status == "loaded":
        return ActionResult(
            extracted_content=(
                f"Tool '{display}' is already loaded — its actions are available now."),
            include_in_memory=True)

    if st.status != "loadable":
        return ActionResult(
            extracted_content=(
                f"load_tool refused (gated:{st.reason}): tool '{display}' — {st.remedy}"),
            include_in_memory=True)

    loaded = await controller.load_tools_from_container([display])
    if display not in (loaded or {}):
        return ActionResult(
            error=(
                f"load_tool: tool '{display}' was loadable but failed to load from "
                f"the container — see session logs"))

    n_actions = len(getattr(loaded[display], "actions", None) or ()) or None
    detail = f" ({n_actions} actions)" if n_actions else ""
    return ActionResult(
        extracted_content=(
            f"Tool '{display}' loaded{detail} — its actions are available from the "
            f"next step."),
        include_in_memory=True)
