"""``app_service`` — a durable app behind a public URL (proposal 032).

The agent verbs (``deploy``/``stop``/``list_apps``/``logs``) only READ and WRITE
registry rows; the owner-owned ``polyrob-apps.service`` (``polyrob apps
supervise``) turns rows into hardened containers behind nginx. Off by default;
ON under effective ``AGENT_BUILDER_MODE=ship``; never in default ``tool_ids``.
"""
from core.app_service.config import app_service_enabled


def register_app_service_tool(force: bool = False) -> bool:
    """Register the 'app_service' descriptor + class IFF ``APP_SERVICE_ENABLED``.
    Mirrors ``tools.publish.register_publish_tool``."""
    from tools.descriptors import ToolCategory, ToolDescriptor, register_optional_tool
    from tools.app_service.tool import AppServiceTool

    return register_optional_tool(
        "app_service",
        AppServiceTool,
        ToolDescriptor(
            name="app_service",
            description="Run a built app as a durable service behind a public URL "
                        "(deploy/stop/list_apps/logs). A NEW slug needs owner approval "
                        "once; the same slug then redeploys unattended within caps.",
            category=ToolCategory.INTEGRATION,
            required_config=[],
            init_priority=85,
            is_optional=True,
        ),
        app_service_enabled,
        force=force,
    )


__all__ = ["app_service_enabled", "register_app_service_tool"]
