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
  missing config or the deploy shape), ``custody-no-browser`` (a browser-bearing
  tool in a wallet-custody process whose remote browser rail is unset or
  unreachable — the remedy is the rail's own line), ``unknown-tool``.

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

# Tools that cannot act without a browser. Under wallet custody they are exactly
# as available as the remote browser rail (core/security/browser_rail.py).
BROWSER_BEARING_TOOLS = frozenset({"browser", "x_browser", "dapp_browser"})


@dataclass(frozen=True)
class ToolStatus:
    """Resolved status for one display tool id."""
    tool_id: str
    status: str          # "loaded" | "loadable" | "gated"
    reason: str = ""     # gated only: money | leaf-blocked | unavailable-on-this-deploy | custody-no-browser | unknown-tool
    remedy: str = ""     # the channel that unblocks it — never empty for gated/loadable


#: 057 WS-A — display tool id -> (verdict kind, the remedy channel). A tool whose
#: CREDENTIALS this deploy has already had rejected is rendered
#: ``gated:credentials-rejected`` even though it is loaded, for the same reason
#: the browser rail is: a tool that refuses 100% of its calls is not loaded, it
#: is gated, and the catalog must say so with the remedy rather than let the run
#: discover it by failing and then misreport the cause.
#:
#: This is what makes ``STABLE_AUTONOMOUS_TOOLSET`` honest: the requested toolset
#: stops flipping on a 900 s TTL (which cost a cold prompt cache every time), and
#: the rejection is told HERE instead of by silently removing the tool.
CREDENTIAL_GATED_TOOLS = {
    "email": ("smtp", "SMTP credentials were rejected (535). The owner must fix "
                      "GMAIL_* / EMAIL_* credentials, or switch EMAIL_PROVIDER to "
                      "agentmail. Do not retry this rail until then — say so and "
                      "use another channel."),
    "twitter": ("twitter_api", "the X API refused this account (402 — credits). "
                               "The owner must top up, or the browser rail "
                               "(x_browser) carries the post instead."),
}


def _credential_verdict_status(display_id: str) -> Optional["ToolStatus"]:
    """``gated:credentials-rejected`` when a live verdict says this rail's
    credentials were refused, else None. Fail-open (an unreadable verdict store
    must never gate a working tool)."""
    row = CREDENTIAL_GATED_TOOLS.get(display_id)
    if row is None:
        return None
    kind, remedy = row
    try:
        if kind == "smtp":
            # Only the SMTP transport can have SMTP credentials rejected; an
            # AgentMail inbox is unaffected by a 535 on the legacy rail.
            from core.config_policy.capability_toggles import email_provider
            if email_provider() != "smtp":
                return None
        from core.credential_verdicts import DEFAULT_TTL_BY_KIND, rejected_within
        if not rejected_within(kind, DEFAULT_TTL_BY_KIND.get(kind, 900.0)):
            return None
    except Exception:
        return None
    return ToolStatus(display_id, "gated", "credentials-rejected", remedy)


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

    # A browser-bearing tool in a custody process is only as available as the
    # remote browser rail. Checked BEFORE `loaded`: a loaded tool that refuses
    # 100% of the time is not loaded, it is gated, and the catalog must say so
    # with the remedy rather than let the agent find out by failing (prod
    # 2026-09-17: the agent tried, failed, and misreported the cause).
    if display in BROWSER_BEARING_TOOLS:
        try:
            from core.security.browser_rail import browser_rail_status
            rail = browser_rail_status()
        except Exception:
            rail = None
        if rail is not None and not rail.usable:
            return ToolStatus(
                display, "gated", "custody-no-browser",
                f"browser rail: {rail.line()}")

    # 057 WS-A: a rejected-credential verdict outranks `loaded` for the same
    # reason the browser rail does — see _credential_verdict_status.
    _verdict = _credential_verdict_status(display)
    if _verdict is not None:
        return _verdict

    if display in loaded_ids:
        return ToolStatus(display, "loaded")

    caps = TOOL_CAPABILITIES.get(display)
    if caps is None and display not in TOOL_DESCRIPTORS:
        return ToolStatus(
            display, "gated", "unknown-tool",
            "not a known tool id — see <tool-catalog> for the valid ids")
    caps = caps or frozenset()

    if "money" in caps:
        # ⚠️ Do NOT reintroduce "request it on the goal payload" here. A goal the
        # agent creates itself can never carry a money tool: goal_create filters
        # the whole money-SPEND set out (tools/goal_tools.py), deliberately, so
        # an injected goal cannot launder itself into a trade. Naming that as
        # the remedy sent the prod agent round a loop — try it, watch it get
        # stripped, file "defi_trade not granted" as an owner ask, repeat — ~50
        # times in two weeks. A refusal naming an impossible remedy is worse
        # than one naming none.
        return ToolStatus(
            display, "gated", "money",
            "money tools are explicit-grant-only. A goal you create yourself can "
            "NEVER carry one — goal_create strips money tools by design, so do "
            "not retry by adding it to your own goal payload. Only the "
            "owner/operator grants it: on a goal THEY seeded (the standing "
            "trading cycle already carries it), or at session creation. If you "
            "have a candidate and no grant, record it where the granted run will "
            "read it and say so plainly — then escalate ONCE, not every run.\n"
            "⚠️ AND, when the OWNER is the one asking: the money capability is "
            "not missing, it is simply not YOURS. It is reachable from their "
            "seat as a chat verb they type themselves — `/trade <what to do>` "
            "for a run that carries the money verb, `/bridge <from> <to> "
            "<amount>` to move native value between chains, `/wallet` for "
            "balances and caps. Say THAT. Do not answer an owner's 'do X' with "
            "a list of grants you would need: naming a capability you cannot "
            "reach as though the system lacked it is a capability DENIAL, and "
            "the owner then believes a shipped feature is broken (live, "
            "2026-09-12: the owner was told the bridge 'has not reached my "
            "toolset' and asked to grant defi_trade, when `/bridge` was "
            "deployed and working the whole time).")

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
        "tool in silence. Use tool_search(\"<keyword>\") to find a tool by name "
        "(including the tools behind connected MCP servers, which are NOT listed "
        "here) and tool_describe(\"<id>\") for its parameters.",
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
