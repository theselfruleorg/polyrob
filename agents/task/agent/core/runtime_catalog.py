"""Refresh live capability facts without rebuilding the stable system prompt."""


def refresh_tool_catalog(agent):
    manager, controller = agent.message_manager, agent.controller
    if getattr(manager, "_tool_catalog_message", None) is None:
        return  # progressive disclosure is not enabled for this session
    if not hasattr(controller, "render_tool_catalog"):
        return
    try:
        content = controller.render_tool_catalog(is_leaf=(getattr(agent, "_role", None) == "leaf"))
    except Exception:
        # Keep running, but never keep advertising a stale snapshot as current.
        content = ("<tool-catalog>Current capability snapshot unavailable. Do not infer "
                   "availability from earlier snapshots; runtime tool and approval gates "
                   "still apply.</tool-catalog>")
    manager.set_tool_catalog_message(content)
