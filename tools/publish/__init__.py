"""``publish`` — give a built page a stable public URL (the ship rail).

``PUBLISH_ENABLED`` is deliberately NOT in the ``POLYROB_LOCAL`` safe-flags
group: putting a file at a PUBLIC address is not a safe local default the way
e.g. ``CODING_TOOLS_ENABLED`` is. An operator opts in, and sets
``PUBLISH_BASE_URL`` to the host that actually serves ``PUBLISH_ROOT``.
"""
from core.env import bool_env as _bool_env


def publish_enabled() -> bool:
    """Register the ``publish`` tool. Default OFF; NOT flipped by POLYROB_LOCAL.
    ON by default under ``AGENT_BUILDER_MODE=build|ship`` (032); explicit env wins.
    The ONE reader is ``core.config_policy.builder_mode.publish_enabled``."""
    from core.config_policy.builder_mode import publish_enabled as _core_publish_enabled
    return _core_publish_enabled()


def register_publish_tool(force: bool = False) -> bool:
    """Register the 'publish' descriptor + class IFF ``PUBLISH_ENABLED``.

    Mirrors ``tools.hf_deploy.register_hf_deploy_tool``. Never in the default
    ``tool_ids`` — the agent (or a goal/cron run) opts in.
    """
    from tools.descriptors import ToolCategory, ToolDescriptor, register_optional_tool
    from tools.publish.tool import PublishTool

    return register_optional_tool(
        "publish",
        PublishTool,
        ToolDescriptor(
            name="publish",
            description="Publish workspace files to a stable public URL "
                        "(publish/unpublish/publish_list). A new slug needs owner "
                        "approval once; the same slug then updates unattended.",
            category=ToolCategory.INTEGRATION,
            required_config=[],
            init_priority=84,
            is_optional=True,
        ),
        publish_enabled,
        force=force,
    )


__all__ = ["publish_enabled", "register_publish_tool"]
