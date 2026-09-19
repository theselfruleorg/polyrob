"""Why did a requested ``tool_id`` fail to load, and how do we say so? (FIX 4)

``Controller.load_tools_from_container`` used to skip a requested tool the
container cannot serve in silence — no exception, no run-visible warning, nothing
on the goal row. The live cost: ``data/streams/streams.yaml`` grants the
``ship-software`` stream ``publish`` unconditionally while production runs with
``PUBLISH_ENABLED`` off, so the seeded goal dispatched WITHOUT ``publish`` and
neither the model nor the owner was ever told why the deliverable had no URL.

This module turns the omission into an attributable sentence. It deliberately
reuses the EXISTING vocabulary rather than inventing a second one:
``tools/tool_disclosure.py::resolve_tool_status`` already renders
``gated:<reason>`` + a remedy channel, and this reads the same
``tools/descriptors.py`` metadata.

The one thing that vocabulary cannot say is the most useful thing here: a tool
gated OFF by a feature flag never even reaches ``TOOL_DESCRIPTORS`` (its
``register_optional_tool`` call is skipped), so ``resolve_tool_status`` reports it
as ``unknown-tool`` — true but useless. :data:`TOOL_GATE_FLAGS` names the flag so
the refusal reads "``PUBLISH_ENABLED`` is off" instead. The map only ENRICHES the
message; an id it does not know still gets the honest descriptor-derived answer.
"""
import logging
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)

#: tool_id -> the env flag whose OFF value explains "this tool is not registered".
#: Every value is checked against the flags catalog by
#: tests/unit/tools/controller/test_tool_load_failures.py, so it cannot rot into
#: naming a flag that no longer exists.
TOOL_GATE_FLAGS: Dict[str, str] = {
    "publish": "PUBLISH_ENABLED",
    "app_service": "APP_SERVICE_ENABLED",
    "hf_deploy": "HF_DEPLOY_ENABLED",
    "github": "GITHUB_TOOL_ENABLED",
    "git": "GIT_TOOLS_ENABLED",
    "coding": "CODING_TOOLS_ENABLED",
    "code_execution": "CODE_EXEC_ENABLED",
    "shell": "SHELL_TOOLS_ENABLED",
    "process": "SHELL_TOOLS_ENABLED",
    "self_env": "SELF_ENV_ENABLED",
    "cronjob": "CRON_ENABLED",
    "goal": "GOALS_ENABLED",
    "knowledge": "KB_ENABLED",
    "x_browser": "X_BROWSER_ENABLED",
    "defi_trade": "DEFI_TRADE_ENABLED",
    "defi_data": "DEFI_DATA_ENABLED",
    "x402_invoice": "X402_INVOICE_ENABLED",
    "mcp": "MCP_ENABLED",
}


#: Ids the GOAL vocabulary grants (`tools/goal_tools.py::_SELF_GOAL_ALLOWED_TOOLS`,
#: proposal 009) that name a controller ACTION registered by
#: `_register_default_actions`, not a container tool object: `message` is the
#: `_register_message_action` verb behind MESSAGE_TOOL_ENABLED. Requesting such an
#: id as a tool_id is satisfied by the action being present; recording it as a
#: missing TOOL wrote a false "message … not a known tool id" line onto every
#: telegram-mentioning goal's result record (prod 2026-09-18) while the very same
#: run used the action. Value = the flag that gates the action, for the honest
#: line when it is genuinely absent.
ACTION_IDS_NOT_TOOLS: Dict[str, str] = {
    "message": "MESSAGE_TOOL_ENABLED",
}


def action_id_gap(tool_id: str, has_action) -> Optional[str]:
    """For an id in :data:`ACTION_IDS_NOT_TOOLS`: ``None`` when the action is
    registered (nothing is missing — do not record a gap), else the
    ``gated:disabled-by-flag`` line naming its flag. For any other id: ``""``
    (caller falls through to the tool probes)."""
    flag = ACTION_IDS_NOT_TOOLS.get((tool_id or "").strip())
    if not flag:
        return ""
    try:
        if has_action(tool_id):
            return None
    except Exception:
        logger.debug("has_action probe failed for %r", tool_id, exc_info=True)
    set_to = os.getenv(flag)
    state = f"{flag}={set_to!r}" if set_to else f"{flag} is off"
    return (f"gated:disabled-by-flag — '{tool_id}' is an agent action, not a tool, "
            f"and it is not registered on this deploy ({state}); the owner enables "
            f"it, the agent cannot")


def _tool_is_registered(tool_id: str) -> bool:
    """Whether the id has a descriptor at all (a flag-gated tool has none)."""
    try:
        from tools.descriptors import TOOL_DESCRIPTORS, get_tool_display_name
        return (get_tool_display_name(tool_id) in TOOL_DESCRIPTORS
                or tool_id in TOOL_DESCRIPTORS)
    except Exception:
        return False


def describe_missing_tool(tool_id: str, *, container=None,
                          loaded_ids=None, is_leaf: bool = False) -> str:
    """One honest ``gated:<reason> — <remedy>`` line for a tool that did NOT load.

    Never raises: an unresolvable id degrades to a plain not-registered line.
    """
    tool_id = (tool_id or "").strip()
    flag = TOOL_GATE_FLAGS.get(tool_id)
    if flag and not _tool_is_registered(tool_id):
        # The tool exists in the codebase but its registration was skipped, so the
        # flag IS the answer. (`os.environ` rather than the per-tool predicate:
        # this is a diagnostic string, and the predicates live behind imports that
        # a degraded deploy may not have.)
        set_to = os.getenv(flag)
        state = f"{flag}={set_to!r}" if set_to else f"{flag} is off"
        return (f"gated:disabled-by-flag — not registered on this deploy "
                f"({state}); the owner enables it, the agent cannot")
    try:
        from tools.tool_disclosure import resolve_tool_status
        st = resolve_tool_status(tool_id, container=container,
                                 loaded_ids=set(loaded_ids or ()), is_leaf=is_leaf)
        if st.status == "gated":
            return f"gated:{st.reason} — {st.remedy}"
        # loadable/loaded here means the probe disagrees with the load attempt
        # (a construction error, not a policy gate) — say exactly that.
        return f"gated:load-failed — the container has '{tool_id}' but it did not initialize"
    except Exception:
        logger.debug("tool-status probe failed for %r", tool_id, exc_info=True)
        return "gated:unavailable-on-this-deploy — not registered in this container"


def format_tool_gap_note(failures: Optional[Dict[str, str]]) -> str:
    """Render recorded failures as ONE line for a run/goal-level surface, or ``""``.

    Kept here (not at the call site) so the goal board, the logs and any future
    surface all read the same sentence.
    """
    if not failures:
        return ""
    parts = "; ".join(f"{tid} [{reason}]" for tid, reason in sorted(failures.items()))
    return f"[tool gap] requested but not registered: {parts}"


__all__ = ["ACTION_IDS_NOT_TOOLS", "TOOL_GATE_FLAGS", "action_id_gap",
           "describe_missing_tool", "format_tool_gap_note"]
