"""`self_env` self-maintenance tool (computer-use parity WS-5, posture 2).

The narrow, audited path for the agent to manage its OWN service — distinct
approvable verbs, NEVER raw bash, so each host-touching action is individually
gated and logged:
- ``self_env_install_dep`` / ``self_env_patch_source`` — create exact, durable
  maintenance proposals only; they never install packages or edit live code;
- ``self_env_read_source`` — reads bounded, install-tree-confined source with
  secrets/config hard-denied;
- ``self_env_restart_service`` / ``self_env_git_pull`` — refuse and direct the
  owner to the trusted update supervisor.

Gated by ``compute_posture_allows(ctx, 2)`` AND (via the Controller's posture-2
approval wiring, WS-6) an owner approval decision. Registered only at
AGENT_COMPUTE_POSTURE>=2; in DELEGATE_BLOCKED_TOOLS; never in the default tool_ids.
"""
from __future__ import annotations

import logging

from core.env import bool_env as _bool_env


def self_env_enabled() -> bool:
    """Whether the `self_env` tool is registered (reachable at posture>=2).

    An explicit ``SELF_ENV_ENABLED`` always wins (e.g. force-off at a raised posture).
    """
    from core.config_policy import posture_flag
    return posture_flag("SELF_ENV_ENABLED", 2)


def register_self_env_tool(force: bool = False) -> bool:
    """Register the `self_env` descriptor + class IFF reachable (or forced).

    No-op below posture 2 so a default/posture-1 deploy is byte-identical. Never in
    the default ``tool_ids``.
    """
    from tools.descriptors import ToolDescriptor, ToolCategory, register_optional_tool
    from tools.self_env.tool import SelfEnvTool

    return register_optional_tool(
        "self_env",
        SelfEnvTool,
        ToolDescriptor(
            name="self_env",
            description="Review agent source and create maintenance proposals; live updates "
                        "require the trusted supervisor (approval-gated)",
            category=ToolCategory.INTEGRATION,
            is_optional=True,
            init_priority=80,
        ),
        self_env_enabled,
        force=force,
    )


__all__ = ["self_env_enabled", "register_self_env_tool"]
