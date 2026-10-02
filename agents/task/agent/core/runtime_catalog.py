"""Refresh live capability facts without rebuilding the stable system prompt.

F8 (063 WS-4). The ``<tool-catalog>`` carries LIVE gate status and is re-rendered
every step, so the original in-place rewrite of foundation slot [7] moved an
early message mid-session: a `load_tool`, an MCP connect, a credential-verdict
opening or closing, or a single render exception re-billed the entire
conversation behind it at uncached prices. The FIRST render stays the session
baseline in [7]; a later change is delivered as ONE durable
``<tool-catalog-update>`` message at the TAIL, naming only the tool lines that
moved. Gated ``TOOL_CATALOG_TAIL_UPDATES`` (default ON); ``false`` restores the
in-place rewrite byte for byte.
"""

# The string the pre-F8 path substituted into [7] when the render raised. Kept
# for the flag-off path only: under F8 a transient render error pushes NOTHING
# (a different string in [7] is exactly the rewrite this change exists to stop),
# and the runtime gates still refuse whatever the stale snapshot advertises.
_RENDER_FAILED = ("<tool-catalog>Current capability snapshot unavailable. Do not infer "
                  "availability from earlier snapshots; runtime tool and approval gates "
                  "still apply.</tool-catalog>")


def announce_late_tools(agent):
    """F9 (063 WS-4) — surface the actions registered AFTER this session's first
    emit, on a model that can accept them mid-conversation.

    Only in ``deferred`` mode: there the late actions ARE in ``tools[]`` carrying
    ``defer_loading``, which keeps them out of the model's context (and out of
    the cached prefix) until a ``tool_addition`` message names them. In ``grow``
    the tools simply appeared, and in ``bridge`` they are reached through
    ``tool_call`` — both push nothing, so this is inert unless the seat is a
    capable Anthropic one.

    ONE durable control message per new batch, body = exactly one action name
    per line (the wire layer parses it: ``modules/llm/deferred_tools.py``). Each
    name is announced once — a second refresh with no new action pushes nothing.
    Fail-open: a broken announcement must never break a step.
    """
    manager, controller = agent.message_manager, agent.controller
    try:
        from modules.llm.deferred_tools import (
            LATE_TOOL_MODE_DEFERRED, late_tool_mode, model_supports_deferred_tools)
        provider = (getattr(agent, "llm_provider", None)
                    or getattr(manager, "provider_name", None) or "")
        if late_tool_mode(provider) != LATE_TOOL_MODE_DEFERRED:
            return
        if not model_supports_deferred_tools(getattr(manager, "model_name", None)):
            return
        registry = getattr(controller, "registry", None)
        names = tuple(registry.late_action_names()) if registry is not None else ()
        if not names:
            return
        announced = getattr(manager, "_tool_additions_announced", None)
        if announced is None:
            announced = set()
            manager._tool_additions_announced = announced
        fresh = [n for n in names if n not in announced]
        if not fresh:
            return
        announced.update(fresh)
        from modules.llm.messages import MessageOrigin, make_control_message
        manager.push_control_message(
            make_control_message("\n".join(fresh), MessageOrigin.TOOL_ADDITION))
        _debug(agent, f"F9: announced {len(fresh)} deferred tool(s): {fresh}")
    except Exception as e:
        _debug(agent, f"F9: late-tool announcement skipped: {e}")


def refresh_tool_catalog(agent):
    manager, controller = agent.message_manager, agent.controller
    # F9: independent of progressive disclosure — an MCP connect registers late
    # actions without a <tool-catalog> ever being pinned.
    announce_late_tools(agent)
    if getattr(manager, "_tool_catalog_message", None) is None:
        return  # progressive disclosure is not enabled for this session
    if not hasattr(controller, "render_tool_catalog"):
        return

    from core.env import bool_env
    tail_updates = bool_env("TOOL_CATALOG_TAIL_UPDATES", True)

    try:
        content = controller.render_tool_catalog(is_leaf=(getattr(agent, "_role", None) == "leaf"))
    except Exception as e:
        if tail_updates:
            _debug(agent, f"tool-catalog render failed; keeping the pinned snapshot: {e}")
            return
        # Legacy path: keep running, but never keep advertising a stale snapshot.
        content = _RENDER_FAILED

    if tail_updates:
        manager.update_tool_catalog(content)
    else:
        manager.set_tool_catalog_message(content)


def _debug(agent, message: str) -> None:
    """Best-effort debug log — a missing logger must never break a step."""
    try:
        agent.logger.debug(message)
    except Exception:
        pass
