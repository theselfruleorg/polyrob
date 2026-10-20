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
TOOL_PROGRESSIVE_DISCLOSURE (default OFF; ON under POLYROB_LOCAL) or, for an
autonomous run, AUTONOMOUS_TOOL_DISCLOSURE (default ON since 2026-10-14, E-4); an
owner chat seat always has it (081 WS-3.1). An autonomous run and an owner seat
reach a late tool through the ``tool_call`` bridge (:func:`session_late_tool_mode`).

081 WS-3.1 — ONE experience on every seat: :data:`CATALOG_INTRO` (the catalog
wording), :func:`load_result_text` (after a load) and :func:`not_loaded_text`
(a call to an action the session does not hold) are the only texts; the remedy
strings live in ``core.tool_gates.REMEDIES``.
"""
import logging
from dataclasses import dataclass
from typing import Iterable, Optional, Set

from core.tool_gates import remedy_text

logger = logging.getLogger(__name__)

# Tools that cannot act without a browser. Under wallet custody they are exactly
# as available as the remote browser rail (core/security/browser_rail.py).
BROWSER_BEARING_TOOLS = frozenset({"browser", "x_browser", "dapp_browser"})


def disclosure_ceiling(controller):
    """Autonomous self-service loading obeys the same ceiling as self-created work."""
    sid = getattr(controller, 'session_id', None)
    if not isinstance(sid, str) or not sid:
        return None
    try:
        from agents.task.session_class import is_autonomous_session
        if not is_autonomous_session(sid):
            return None
        # A standing cron job or goal carries standing authority (CHAT-5): it
        # loads what its task needs, like the owner's own chat. Money tools
        # stay explicit-grant-only (checked before the ceiling).
        from core.autonomy_marker import job_task_for_session
        if job_task_for_session(sid):
            return None
        from tools.goal_tools import allowed_self_goal_tools
        from tools.descriptors import get_tool_display_name
        return frozenset(get_tool_display_name(tool) for tool in allowed_self_goal_tools())
    except Exception:
        return frozenset()


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
}


def register_credential_gate(tool_id: str, kind: str, remedy: str) -> None:
    """A pack tool's row in :data:`CREDENTIAL_GATED_TOOLS` (067 P3b: the X pack
    registers ``twitter`` -> ``twitter_api`` from its registrar). ``kind`` is a
    ``core.credential_verdicts`` verdict kind. Last registration wins."""
    CREDENTIAL_GATED_TOOLS[tool_id] = (kind, remedy)


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
    allowed_ids: Optional[Set[str]] = None,
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
        return ToolStatus(display, "gated", "unknown-tool", remedy_text("unknown-tool"))
    caps = caps or frozenset()

    if "money" in caps:
        # One predicate picks the text (core.tool_gates REMEDIES "money" vs
        # "money_armed" — the ⚠️ history of both lives there): when the money
        # regime is ARMED the self-goal ceiling holds the tool and goal_create
        # keeps it, so "a goal you create can NEVER carry one" would be false.
        try:
            from tools.goal_tools import allowed_self_goal_tools
            _self_grantable = display in allowed_self_goal_tools()
        except Exception:
            _self_grantable = False
        return ToolStatus(display, "gated", "money", remedy_text(
            "money_armed" if _self_grantable else "money", tool=display)
            + "\n" + remedy_text("money_owner_move"))

    if allowed_ids is not None and display not in allowed_ids:
        return ToolStatus(display, 'gated', 'autonomous-ceiling',
                          remedy_text('autonomous-ceiling'))

    if is_leaf:
        from tools.controller.delegation import get_blocked_child_tools
        if display in get_blocked_child_tools():
            return ToolStatus(display, "gated", "leaf-blocked", remedy_text("leaf-blocked"))

    if _container_has_tool(container, display):
        return ToolStatus(display, "loadable", "", remedy_text("load_tool", tool=display))

    gate = _gate_line(display)
    if gate:
        # 081 bar 2: the tool's own gate (the flag, the posture limb that is
        # actually unmet) — the catalog owns tool status; <tool-availability>
        # does not repeat it when the catalog is pinned.
        return ToolStatus(display, "gated", "unavailable-on-this-deploy", gate)
    desc = TOOL_DESCRIPTORS.get(display) or TOOL_DESCRIPTORS.get(tool_id)
    required = list(getattr(desc, "required_config", None) or [])
    remedy = (remedy_text("needs-config", flag=", ".join(required)) if required
              else remedy_text("not-constructed"))
    return ToolStatus(display, "gated", "unavailable-on-this-deploy", remedy)


def _gate_line(display: str) -> str:
    """``"<gate> → <remedy>"`` from the tool's disclosed gate (core row or
    pack.toml; the compute tools name the limb that is unmet), else ``""``."""
    try:
        from core.tool_capabilities import row_field
        gate = row_field(display, "gate")
        disc = gate.disclosure(display) if gate is not None else None
        if not disc or disc[1] == "reserved":
            return ""
        label, _tier, remedy = disc
        from agents.task.agent.core.tool_availability import _COMPUTE_TOOLS, compute_gate
        if display in _COMPUTE_TOOLS:
            label, _tier, remedy = compute_gate(display)
        return f"gate: {label} → {remedy}"
    except Exception:
        return ""


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


#: 081 WS-3.1 — the ONE wording of the catalog, the same on every seat (owner
#: chat, cron, goal, leaf): the agent holds only part of the catalog, and
#: load_tool is the one way to the rest. It names no late-tool mode on purpose;
#: the load result (:func:`load_result_text`) says how to call what was loaded.
CATALOG_INTRO = (
    "Every tool this deployment knows, with its status for this session. You hold "
    "only the [loaded] tools now; the catalog lists more. When the task needs a tool "
    "you do not hold, call load_tool(\"<id>\") on a [loadable] one: the result says "
    "how to call its actions. A [gated:...] tool does not load here: it names the "
    "reason and the remedy; follow the remedy or tell the owner, and never work "
    "around a missing tool in silence. tool_search(\"<keyword>\") finds a tool by "
    "keyword, including the tools behind connected MCP servers (not listed here); "
    "tool_describe(\"<id>\") gives its actions and parameters.")


def how_to_call(display: str, bridged: bool) -> str:
    """How the agent calls the actions of a tool loaded mid-session — the ONE
    sentence the load result and ``tool_describe`` both print. In BRIDGE mode
    (F9) the emitted schema list is pinned, so a late action never joins
    ``tools[]``: it is reached by name through ``tool_call``. ``grow`` and
    ``deferred`` do show it (deferred via a tool_addition message)."""
    if bridged:
        return (f'call its actions through tool_call(name="<action>", arguments={{...}}); '
                f'tool_describe("{display}") gives the action names and parameters')
    return "its actions are in your tool list from the next step"


def load_result_text(display: str, n_actions: Optional[int], bridged: bool) -> str:
    """The ``load_tool`` success text (one wording on every seat). It is a tool
    result in this turn, so the agent may tell the owner it loaded the tool."""
    detail = f" ({n_actions} actions)" if n_actions else ""
    return f"Tool '{display}' loaded{detail}: {how_to_call(display, bridged)}."


def action_tool_id(action_name: str) -> Optional[str]:
    """The display tool id an action name belongs to (``email_send`` -> ``email``),
    by the longest known tool id that prefixes it; None when no tool owns it."""
    from core.tool_capabilities import TOOL_CAPABILITIES
    from tools.descriptors import TOOL_DESCRIPTORS, get_tool_display_name
    name = (action_name or "").strip()
    best = None
    for tid in set(TOOL_CAPABILITIES) | {get_tool_display_name(t) for t in TOOL_DESCRIPTORS}:
        if (name == tid or name.startswith(tid + "_")) and (best is None or len(tid) > len(best)):
            best = tid
    return get_tool_display_name(best) if best else None


def not_loaded_text(action_name: str, controller, *, is_leaf: bool = False) -> str:
    """081 WS-3.1 — the ONE refusal for a call to an action this session does not
    hold (a native tool call the registry rejects, or ``tool_call``): it names
    the tool, and ``load_tool`` — or, for money, the owner grant — as the way."""
    name = (action_name or "").strip()
    tid = action_tool_id(name)
    if tid is None:
        return (f"'{name}' is not an action you hold, and no tool in this deployment "
                f"owns it. tool_search(\"<keyword>\") finds the right tool.")
    st = resolve_tool_status(
        tid, container=getattr(controller, "container", None),
        loaded_ids=set(controller.list_tools() or ()), is_leaf=is_leaf,
        allowed_ids=disclosure_ceiling(controller))
    if st.status == "loaded":
        return (f"'{name}' is not an action of the loaded tool '{tid}'. "
                f"tool_describe(\"{tid}\") lists its actions.")
    if st.status == "loadable":
        return (f"'{name}' is not loaded: it belongs to the tool '{tid}'. Call "
                f"load_tool(\"{tid}\") first; the result says how to call it.")
    return f"'{name}' is not loaded: its tool '{tid}' is gated:{st.reason} — {st.remedy}"


def render_tool_catalog(
    *,
    container,
    loaded_ids: Iterable[str],
    is_leaf: bool = False,
    allowed_ids: Optional[Set[str]] = None,
) -> str:
    """Render the compact ``<tool-catalog>`` block — one line per display tool.

    Pure render over the existing SSOTs; iterates the descriptor init order and
    dedupes runtime aliases (``browser_manager`` renders once, as ``browser``).
    """
    from tools.descriptors import (
        TOOL_DESCRIPTORS, get_tool_init_order, get_tool_display_name)

    loaded = set(loaded_ids or ())
    lines = ["<tool-catalog>", CATALOG_INTRO]
    seen = set()
    money_notes: dict = {}   # the money remedy, stated ONCE below the lines (B-8)
    for name in get_tool_init_order():
        display = get_tool_display_name(name)
        if display in seen:
            continue
        seen.add(display)
        st = resolve_tool_status(
            display, container=container, loaded_ids=loaded, is_leaf=is_leaf, allowed_ids=allowed_ids)
        desc = _one_liner(TOOL_DESCRIPTORS[name].description)
        if st.status == "gated" and st.reason == "money":
            money_notes.setdefault(st.remedy.replace(f"'{display}'", "'<id>'"), []).append(display)
            lines.append(f"- {display}: {desc} [gated:money — see the money note]")
            continue
        lines.append(f"- {display}: {desc} {_status_suffix(st)}")
    # A gated tool with no descriptor (e.g. x402_pay before it is constructed)
    # still gets its one catalog line, so no tool status lives anywhere else.
    from core.tool_capabilities import gate_disclosure_map
    for display in sorted(set(gate_disclosure_map()) - seen):
        st = resolve_tool_status(
            display, container=container, loaded_ids=loaded, is_leaf=is_leaf, allowed_ids=allowed_ids)
        if st.status == "gated" and st.reason == "money":
            money_notes.setdefault(st.remedy.replace(f"'{display}'", "'<id>'"), []).append(display)
            lines.append(f"- {display}: [gated:money — see the money note]")
        else:
            lines.append(f"- {display}: {_status_suffix(st)}")
    move = "\n" + remedy_text("money_owner_move")
    for text, tools in money_notes.items():
        lines.append(f"Money note ({', '.join(tools)}): {text.replace(move, '')}")
    if money_notes:
        lines.append(move.strip())
    lines.append("</tool-catalog>")
    return "\n".join(lines)


async def perform_load_tool(controller, tool_id: str, execution_context=None):
    """Decision + load for the ``load_tool`` action (the closure stays thin —
    mirrors ``perform_message_send``).

    Refusals are STRUCTURED results (reason + remedy), not errors — a policy
    refusal is an answer, not a retryable failure. Only a load that *should*
    have worked and didn't returns an error result.
    """
    from agents.task.loop_types.controller_types import ActionResult
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
        is_leaf=is_leaf, allowed_ids=disclosure_ceiling(controller))

    if st.status == "loaded":
        return ActionResult(
            extracted_content=(
                f"Tool '{display}' is already loaded — its actions are available now."
                + (f" {how_to_call(display, True)}." if _bridge_armed(controller) else "")),
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
    return ActionResult(
        extracted_content=load_result_text(display, n_actions, _bridge_armed(controller)),
        include_in_memory=True)


# E-4: the per-session late-tool resolver lives in a leaf module (the registry
# and the tool_call bridge import it; this module is inside the big import cycle).
from tools.late_tool_mode import (  # noqa: E402,F401
    autonomous_bridge, disclosure_bridge, session_late_tool_mode)


def _bridge_mode(controller=None) -> bool:
    """Is the deployment (or this session: autonomous or an owner seat) in F9
    BRIDGE mode? Fail-open to False.

    Not "is a freeze recorded" — `deferred` mode records one too, and its late
    actions ARE emitted, so the bridge wording would be a lie there.
    """
    try:
        from modules.llm.deferred_tools import late_tool_mode, LATE_TOOL_MODE_BRIDGE
        if late_tool_mode("") == LATE_TOOL_MODE_BRIDGE:
            return True
        return disclosure_bridge(getattr(controller, "session_id", None))
    except Exception:
        return False


def _bridge_armed(controller) -> bool:
    """Is BRIDGE mode on AND this session's emitted ``tools[]`` already pinned?"""
    if not _bridge_mode(controller):
        return False
    try:
        return bool(controller.registry.schemas_frozen())
    except Exception:
        return False


#: Back-compat name for the pre-2026-09-23 helper (tests monkeypatch by name).
_schemas_frozen = _bridge_armed
