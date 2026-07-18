"""Regression (P2 finalization): Controller.remove_tool unconditionally computed the
action key as f"{name}_{action}", but registration namespaces conditionally — a tool
whose action is ALREADY tool-prefixed (e.g. perplexity/perplexity_search) registers as
`perplexity_search`. So remove_tool tried to remove `perplexity_perplexity_search` and
left the real action registered/callable while list_tools reported the tool gone.
"""
import types

import pytest
from pydantic import BaseModel

from tools.controller._helpers import ToolInfo


def _make_controller(tmp_path):
    import agents.task.agent.service  # noqa: F401 — avoid import cycle
    from tools.controller.service import Controller
    orch = types.SimpleNamespace(session_id="s1", user_id="u1", workspace_dir=str(tmp_path))
    container = types.SimpleNamespace(config=types.SimpleNamespace(data_dir=str(tmp_path)))
    return Controller(container=container, orchestrator=orch)


class _P(BaseModel):
    pass


def test_remove_tool_removes_already_prefixed_action(tmp_path):
    c = _make_controller(tmp_path)

    async def _fn(params, execution_context=None):
        return None

    # Register exactly as add_tool would for an already-tool-prefixed action.
    c.registry.wrap_function(name="perplexity_search", function=_fn,
                             description="d", tool="perplexity", param_model=_P)
    c._tools["perplexity"] = ToolInfo(instance=object(),
                                      actions={"perplexity_search": _fn}, name="perplexity")
    assert c.registry.get_action("perplexity_search") is not None

    c.remove_tool("perplexity")
    assert c.registry.get_action("perplexity_search") is None, (
        "remove_tool must remove the actual (conditionally-namespaced) action key"
    )
    assert "perplexity" not in c._tools
