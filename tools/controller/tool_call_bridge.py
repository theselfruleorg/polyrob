"""F9 (063 WS-4) — the ``tool_call`` bridge: reach a LATE tool without growing
``tools[]``.

``load_tool`` (and an MCP connect) registers new actions mid-session. That
changes the emitted provider schema list, and every provider cache serves only
the leading bytes two requests share — on Anthropic the wire order is
``tools -> system -> messages``, so a grown tool array colds the ENTIRE request.
Under ``POLYROB_LOCAL`` progressive disclosure is ON, so the first ``load_tool``
of every local session was a full miss.

Shape (a) of the two in proposal 063 §7 Q1 (the schema-freeze bridge; the alternative
was Pi's Anthropic-only ``defer_loading``).

⚠️ This is the OPT-IN shape, not the default (owner decision, 2026-09-23). The
default late-tool mode is ``grow`` — the emitted list grows on ``load_tool``,
as it did before F9 — or ``deferred`` (shape (b), Anthropic's native
``defer_loading`` + ``tool_addition``) on a capable Anthropic model. The ONE
resolver is ``modules/llm/deferred_tools.py::late_tool_mode``. Behind
``TOOL_SCHEMAS_FROZEN`` (default OFF) the emitted list is pinned to the actions
present at the first emit plus the three discovery verbs ``tool_search`` /
``tool_describe`` / ``tool_call``, on EVERY provider.

A late tool is still fully registered in the Registry, so
validation, permissions, the money/high-impact/delegate gates and
``tool_describe`` all keep working — only the SCHEMA stops being advertised, and
the model calls it by name through ``tool_call(name, arguments)``.

The trade-off shape (a) accepts: a late tool loses NATIVE schema validation. The
bridge validates ``arguments`` against the action's own param model and hands
the validation error back as an ordinary tool result.

⚠️ LANDMINE: this module MUST NOT gain ``from __future__ import annotations`` —
the Registry routes the validated param model by introspecting the action
closure's first-param annotation (``params: ToolCallAction``); stringizing it
breaks the routing. Same rule as ``action_registration.py``.

⚠️ There is ONE dispatch path. ``perform_tool_call`` resolves the target and
re-enters ``Controller.multi_act`` with it, so the pre-tool-call hooks (the
operator denylist, the wallet authority / money refusal, every approval lane),
the turn-origin checks, the timeout, the retry ceiling and the telemetry all fire
against the REAL target action — not against ``tool_call``. Never add a second
dispatch here.
"""
from typing import Any, Dict

from pydantic import BaseModel, Field

#: The bridge verb's action name (the Registry reads this to decide whether the
#: schema freeze may arm at all — freezing without the bridge would strand every
#: late tool).
TOOL_CALL_ACTION = "tool_call"


class ToolCallAction(BaseModel):
    name: str = Field(
        ...,
        description=("Exact action name to call, as shown by tool_describe / "
                     "tool_search (e.g. 'code_execution_run_code')"))
    arguments: Dict[str, Any] = Field(
        default_factory=dict,
        description=("The target action's own parameters as a JSON object. Run "
                     "tool_describe first if you are unsure of the shape."))


def bridge_enabled() -> bool:
    """Whether the frozen-schema bridge is armed for new sessions (F9).

    Default OFF since 2026-09-23 — shape (a) is opt-in and the default late-tool
    mode is ``grow`` (or ``deferred`` on a capable Anthropic model).
    """
    from core.env import bool_env
    return bool_env("TOOL_SCHEMAS_FROZEN", False)


class ToolCallBridgeMixin:
    """Registers and serves the ``tool_call`` bridge verb on the Controller."""

    def _register_tool_call_bridge(self) -> None:
        """Register ``tool_call`` when ``TOOL_SCHEMAS_FROZEN`` is on (opt-in).

        Registered from ``Controller.__init__`` (``action_registration.py`` is AT
        its size ceiling — same escape hatch as ``autonomy_control``/``avatar``),
        and BEFORE the first emit, so the Registry can arm the freeze.
        """
        if not bridge_enabled():
            return

        @self.registry.action(
            "Call an action that is NOT in your tool list because it was loaded "
            "after this session started (load_tool, an MCP connect). Give the "
            "exact action name and its parameters as `arguments`; run "
            "tool_describe(<tool id>) first for the shape. The call runs through "
            "the same permission, approval and money gates a direct call runs "
            "through.",
            param_model=ToolCallAction,
        )
        async def tool_call(params: ToolCallAction, execution_context=None):
            return await perform_tool_call(
                self, params.name, params.arguments,
                execution_context=execution_context)


async def perform_tool_call(controller, name: str, arguments, execution_context=None):
    """Resolve *name*, validate *arguments*, then dispatch through the ONE path.

    Refusals are structured results (an answer the model can act on), never
    errors — a mis-shaped call is not a retryable failure.
    """
    from tools.controller.types import ActionResult

    target = (name or "").strip()
    args = dict(arguments or {})

    if not target:
        return ActionResult(
            extracted_content=("tool_call refused: `name` is required — pass the exact "
                               "action name from tool_describe / tool_search."),
            include_in_memory=True)

    if target == TOOL_CALL_ACTION:
        return ActionResult(
            extracted_content=("tool_call refused: tool_call cannot call itself. Name the "
                               "real action instead."),
            include_in_memory=True)

    action = controller.registry.get_action(target)
    if action is None:
        return ActionResult(
            extracted_content=(
                f"tool_call refused: no action named '{target}' is registered in this "
                f"session. Use tool_search(\"<keyword>\") to find the tool, "
                f"load_tool(\"<id>\") if it is [loadable], then tool_describe(\"<id>\") "
                f"for the exact action name and parameters."),
            include_in_memory=True)

    # Native schema validation is what a late tool LOSES under the freeze; this is
    # where it is paid back. The error is a result, not an exception.
    param_model = getattr(action, "param_model", None)
    if param_model is not None:
        try:
            param_model(**args)
        except Exception as e:
            return ActionResult(
                extracted_content=(
                    f"tool_call: '{target}' rejected these arguments — {e}\n"
                    f"Run tool_describe for the exact parameter shape and call again."),
                include_in_memory=True)

    try:
        ActionModel = controller.create_action_model()
        bridged = ActionModel(**{target: args})
    except Exception as e:
        return ActionResult(
            extracted_content=(
                f"tool_call: could not build a call to '{target}' — {e}"),
            include_in_memory=True)

    # THE dispatch. multi_act runs the pre-tool-call hooks (denylist, wallet
    # authority, every approval lane) against the TARGET name, then act() ->
    # registry.execute_action with the same timeout, retry ceiling and telemetry a
    # native call gets. A hook denial comes back as the ActionResult error it
    # always was.
    results = await controller.multi_act([bridged], execution_context=execution_context)
    if not results:
        return ActionResult(
            error=f"tool_call: '{target}' produced no result", include_in_memory=True)
    return results[0]
