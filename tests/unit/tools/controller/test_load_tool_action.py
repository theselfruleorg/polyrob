"""S2 (dynamic tool rig): the load_tool action is registered iff
TOOL_PROGRESSIVE_DISCLOSURE is on, and routes through perform_load_tool
(decision logic covered in tests/unit/tools/test_tool_disclosure.py).
"""
import types

import pytest


def _make_controller(tmp_path):
    import agents.task.agent.service  # noqa: F401 — avoid controller<->orchestrator import cycle
    from tools.controller.service import Controller

    orch = types.SimpleNamespace(session_id="s1", user_id="u1", workspace_dir=str(tmp_path))
    container = types.SimpleNamespace(config=types.SimpleNamespace(data_dir=str(tmp_path)))
    return Controller(container=container, orchestrator=orch)


def test_load_tool_registered_when_flag_on(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_PROGRESSIVE_DISCLOSURE", "true")
    c = _make_controller(tmp_path)
    assert c.has_action("load_tool")


def test_load_tool_absent_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_PROGRESSIVE_DISCLOSURE", "false")
    c = _make_controller(tmp_path)
    assert not c.has_action("load_tool")


def test_controller_renders_tool_catalog(tmp_path, monkeypatch):
    """The renderer is exposed as a Controller method so the agents tier can pin
    the catalog without importing the tools tier (layering ratchet)."""
    monkeypatch.setenv("TOOL_PROGRESSIVE_DISCLOSURE", "true")
    c = _make_controller(tmp_path)
    cat = c.render_tool_catalog()
    assert cat.startswith("<tool-catalog>")
    assert "filesystem" in cat


@pytest.mark.asyncio
async def test_load_tool_action_refuses_money_tool(tmp_path, monkeypatch):
    """End-to-end through the registered closure: a money tool is refused with a
    structured gated:money result, never loaded."""
    monkeypatch.setenv("TOOL_PROGRESSIVE_DISCLOSURE", "true")
    c = _make_controller(tmp_path)
    action = c.registry.get_action("load_tool")
    params = action.param_model(tool_id="hyperliquid")
    res = await action.function(params, execution_context=None)
    assert "gated:money" in (res.extracted_content or "")
