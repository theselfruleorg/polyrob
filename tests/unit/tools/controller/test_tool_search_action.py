"""Tier-3 item 1: tool_search / tool_describe actions ride the dynamic tool rig
(TOOL_PROGRESSIVE_DISCLOSURE). This file covers the Controller seams + the
registered closures; the pure search/describe logic is in
tests/unit/tools/test_tool_search.py.
"""
import types

import pytest


def _make_controller(tmp_path):
    import agents.task.agent.service  # noqa: F401 — avoid controller<->orchestrator cycle
    from tools.controller.service import Controller

    orch = types.SimpleNamespace(session_id="s1", user_id="u1", workspace_dir=str(tmp_path))
    container = types.SimpleNamespace(config=types.SimpleNamespace(data_dir=str(tmp_path)))
    return Controller(container=container, orchestrator=orch)


def _controller_with_mcp(tmp_path, rows):
    """rows: list of (server, canonical_name, server_tool_name, description, input_schema)."""
    c = _make_controller(tmp_path)
    md = [types.SimpleNamespace(name=n, server_tool_name=stn, description=d,
                                input_schema=sch, server_name=srv)
          for (srv, n, stn, d, sch) in rows]
    grouped = {}
    for m in md:
        grouped.setdefault(m.server_name, []).append(m)
    sm = types.SimpleNamespace(get_all_tools=lambda: grouped)
    c._tools["mcp"] = types.SimpleNamespace(
        instance=types.SimpleNamespace(server_manager=sm))
    return c


# --- Task 1: iter_mcp_tools_metadata seam ------------------------------------

def test_iter_mcp_tools_metadata_flattens_and_deepcopies(tmp_path):
    live = {"k": "v"}
    c = _controller_with_mcp(
        tmp_path, [("anysite", "anysite_search", "search", "web search", live)])
    out = c.iter_mcp_tools_metadata()
    assert out == [{"server": "anysite", "name": "anysite_search",
                    "server_tool_name": "search", "description": "web search",
                    "input_schema": {"k": "v"}}]
    out[0]["input_schema"]["k"] = "MUTATED"
    assert live == {"k": "v"}, "must deep-copy input_schema (live MCP metadata)"


def test_iter_mcp_tools_metadata_empty_without_mcp(tmp_path):
    c = _make_controller(tmp_path)
    assert c.iter_mcp_tools_metadata() == []


# --- Task 5: registered actions + catalog hint -------------------------------

def test_actions_registered_when_flag_on(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_PROGRESSIVE_DISCLOSURE", "true")
    c = _make_controller(tmp_path)
    assert c.has_action("tool_search") and c.has_action("tool_describe")


def test_actions_absent_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_PROGRESSIVE_DISCLOSURE", "false")
    c = _make_controller(tmp_path)
    assert not c.has_action("tool_search") and not c.has_action("tool_describe")


@pytest.mark.asyncio
async def test_tool_search_closure_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_PROGRESSIVE_DISCLOSURE", "true")
    c = _make_controller(tmp_path)
    a = c.registry.get_action("tool_search")
    res = await a.function(a.param_model(query="filesystem"), execution_context=None)
    assert "filesystem" in (res.extracted_content or "")


@pytest.mark.asyncio
async def test_tool_describe_closure_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_PROGRESSIVE_DISCLOSURE", "true")
    c = _make_controller(tmp_path)
    a = c.registry.get_action("tool_describe")
    res = await a.function(a.param_model(tool_id="x402_pay"), execution_context=None)
    assert "money" in (res.extracted_content or "").lower()


def test_catalog_header_mentions_tool_search(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_PROGRESSIVE_DISCLOSURE", "true")
    c = _make_controller(tmp_path)
    assert "tool_search" in c.render_tool_catalog()


# --- Task 6: leaf safety (read-only actions are allowed for a leaf) ----------

@pytest.mark.asyncio
async def test_leaf_can_search_and_describe(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOL_PROGRESSIVE_DISCLOSURE", "true")
    c = _make_controller(tmp_path)
    ctx = types.SimpleNamespace(is_sub_agent=True, role="leaf")
    a = c.registry.get_action("tool_search")
    res = await a.function(a.param_model(query="cronjob"), execution_context=ctx)
    assert not res.error
    assert "leaf-blocked" in (res.extracted_content or "")
    d = c.registry.get_action("tool_describe")
    dres = await d.function(d.param_model(tool_id="cronjob"), execution_context=ctx)
    assert not dres.error
    assert "leaf-blocked" in (dres.extracted_content or "")
