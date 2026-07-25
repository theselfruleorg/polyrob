"""Registration of the dynamic-tool-rig actions — ``load_tool`` (S2) plus the
Tier-3 ``tool_search`` / ``tool_describe`` discovery pair.

Extracted to its own module so ``action_registration.py`` (a god-file under the
size ratchet) does not grow: the decision/render logic lives in
``tools/tool_disclosure.py`` (load) and ``tools/tool_search.py`` (search/describe),
and this module holds only the thin registration seam, called from
``ActionRegistrationMixin._register_default_actions`` behind the
``TOOL_PROGRESSIVE_DISCLOSURE`` gate.

⚠️ LANDMINE: this module MUST NOT gain ``from __future__ import annotations`` —
the Registry routes the validated param model by introspecting each action
closure's first-param annotation (``params: ToolSearchAction``); stringizing it
breaks the routing. Same rule as ``action_registration.py``.
"""
from pydantic import BaseModel, Field


class LoadToolAction(BaseModel):
    tool_id: str = Field(
        ..., description="Tool id from <tool-catalog> to load into this session")


class ToolSearchAction(BaseModel):
    query: str = Field(
        ..., description="Keyword(s) to match against tool ids and descriptions")
    limit: int = Field(8, ge=1, le=25, description="Max results to return (1-25)")


class ToolDescribeAction(BaseModel):
    tool_id: str = Field(
        ..., description="Tool id (built-in) or MCP tool name from tool_search")


def register_dynamic_tool_actions(controller) -> None:
    """Register ``load_tool`` + ``tool_search`` + ``tool_describe`` on *controller*.

    - ``load_tool`` (S2): materializes a ``loadable`` tool mid-session through the
      SAME ``load_tools_from_container`` path session creation uses; decision logic
      (money never loadable, leaf blocklist, structured refusals) lives in
      ``tools/tool_disclosure.py::perform_load_tool``.
    - ``tool_search`` / ``tool_describe`` (Tier-3 item 1): read-only keyword
      discovery over the SAME honest inventory the ``<tool-catalog>`` renders —
      built-ins PLUS the tools behind connected MCP servers (the actual scale).
      Safe for leaves; logic in ``tools/tool_search.py``.
    """
    @controller.registry.action(
        "Load a [loadable] tool from the <tool-catalog> into this session — its "
        "actions become available from the next step. Gated tools return the "
        "reason and the remedy channel instead of loading.",
        param_model=LoadToolAction,
    )
    async def load_tool(params: LoadToolAction, execution_context=None):
        from tools.tool_disclosure import perform_load_tool
        return await perform_load_tool(
            controller, params.tool_id, execution_context=execution_context)

    @controller.registry.action(
        "Search every tool this deployment knows about — built-ins AND the tools "
        "behind connected MCP servers — by keyword. Read-only. Each hit shows "
        "where it lives and its honest status (loaded/loadable/gated). Use "
        "tool_describe for full detail on one hit.",
        param_model=ToolSearchAction,
    )
    async def tool_search(params: ToolSearchAction, execution_context=None):
        from tools.tool_search import perform_tool_search
        return await perform_tool_search(
            controller, params.query, params.limit, execution_context=execution_context)

    @controller.registry.action(
        "Full detail for ONE tool id from tool_search / the <tool-catalog>: its "
        "parameters, capability dimensions, honest status + remedy, and how to "
        "invoke it (direct action, load_tool first, or the mcp_execute_tool "
        "shape). Read-only.",
        param_model=ToolDescribeAction,
    )
    async def tool_describe(params: ToolDescribeAction, execution_context=None):
        from tools.tool_search import perform_tool_describe
        return await perform_tool_describe(
            controller, params.tool_id, execution_context=execution_context)
