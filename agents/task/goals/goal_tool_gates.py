"""032 — which ship rails an autonomous goal run may carry by default.

Kept out of ``dispatcher.py`` (size ratchet). Reads only ``core`` (the agents
tier may not import ``tools``). Explicit ``payload.tools`` on a goal always win
over these defaults (``resolve_tools`` precedence).
"""


def publish_goal_tool_enabled() -> bool:
    """``publish`` joins the default goal toolset under ``AGENT_BUILDER_MODE=build|ship``
    when the rail is on. A NEW slug from a goal run still lands in ``.pending/``
    for the owner; an approved slug iterates unattended."""
    from core.config_policy.builder_mode import effective_builder_mode, publish_enabled
    return effective_builder_mode() in ("build", "ship") and publish_enabled()


def app_service_goal_tool_enabled() -> bool:
    """``app_service`` joins the default goal toolset only under EFFECTIVE ``ship``
    (domain + certificate present) when the rail is on."""
    from core.app_service.config import app_service_enabled
    from core.config_policy.builder_mode import effective_builder_mode
    return effective_builder_mode() == "ship" and app_service_enabled()
