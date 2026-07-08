"""T3-01/03/04/05 (2026-07-06 structural review): the only real self-tooling
mechanism — tools/mcp/self_install.py::perform_mcp_install (gate → allowlist →
screen → approve → add_server → persist) — was fully built with ZERO callers.

Wired as the `mcp_install` action:
- gated MCP_SELF_INSTALL_ENABLED (default OFF — action not even registered);
- forged/leaf turns refused (owner in the loop);
- approver resolved EXPLICITLY: Deny-by-default unless APPROVAL_PROVIDER is set
  (the enable flag alone never silently auto-approves);
- T3-03: catalog entries loadable from a reviewed file (MCP_INSTALL_CATALOG_FILE)
  — the env allowlist alone was a dead seam (id allowed but no entry to install);
- T3-04: persisted via the tenant-scoped user_mcp_service (never the global
  config/mcp_config.json);
- T3-05: direct {server}_{tool} actions re-registered post-install so the new
  tools are callable the SAME session;
- event_log audit row per attempt.
"""
import asyncio
import json
import logging
import types

import pytest

import agents.task.agent.service  # noqa: F401 — import-cycle guard
from agents.task.telemetry.event_log import TelemetryEventLog
from tools.controller.registry.service import Registry
from tools.controller.service import Controller


@pytest.fixture()
def log(tmp_path, monkeypatch):
    lg = TelemetryEventLog(str(tmp_path / "telemetry_events.db"))
    monkeypatch.setattr(
        "agents.task.telemetry.event_log.get_event_log", lambda db_path=None: lg
    )
    monkeypatch.delenv("TELEMETRY_EVENT_LOG_ENABLED", raising=False)
    return lg


class _FakeServerManager:
    def __init__(self):
        self.added = []

    async def add_server(self, server_id, config):
        self.added.append(server_id)
        return True


class _FakeUMS:
    def __init__(self):
        self.persisted = []

    async def add_server(self, user_id, server_name, server_url, server_type="sse", **kw):
        self.persisted.append((user_id, server_name, server_url))
        return types.SimpleNamespace(success=True, error=None)


def _controller(monkeypatch, *, enabled=True, with_mcp_tool=True, ums=None):
    if enabled:
        monkeypatch.setenv("MCP_SELF_INSTALL_ENABLED", "true")
    else:
        monkeypatch.delenv("MCP_SELF_INSTALL_ENABLED", raising=False)
    c = object.__new__(Controller)
    c.logger = logging.getLogger("mcp-install-test")
    c.registry = Registry()
    c.user_id = "u1"
    c.session_id = "s1"
    c._tools = {}
    sm = _FakeServerManager()
    if with_mcp_tool:
        mcp_tool = types.SimpleNamespace(server_manager=sm)
        c._tools["mcp"] = types.SimpleNamespace(instance=mcp_tool)
    c.container = types.SimpleNamespace(
        get_service=lambda name: (ums if name == "user_mcp_service" else None),
        config=types.SimpleNamespace(data_dir="data"),
    )
    reregistered = []

    async def _rereg(tool):
        reregistered.append(tool)

    c._register_mcp_tools_as_direct_actions = _rereg
    c._register_mcp_install_action()
    return c, sm, reregistered


def _ctx(**kw):
    base = dict(user_id="u1", session_id="s1", is_sub_agent=False,
                role="orchestrator", metadata={})
    base.update(kw)
    return types.SimpleNamespace(**base)


def _run(c, params_kw, ctx=None):
    action = c.registry.registry.actions["mcp_install"]
    params = action.param_model(**params_kw)
    return asyncio.new_event_loop().run_until_complete(
        action.function(params, execution_context=ctx or _ctx()))


# ------------------------------------------------------------------ gating

def test_not_registered_when_flag_off(monkeypatch):
    c, _, _ = _controller(monkeypatch, enabled=False)
    assert "mcp_install" not in c.registry.registry.actions


def test_registered_when_flag_on(monkeypatch):
    c, _, _ = _controller(monkeypatch)
    assert "mcp_install" in c.registry.registry.actions


# ------------------------------------------------------------------ T3-03 catalog file

def test_catalog_loads_reviewed_file_entries(tmp_path, monkeypatch):
    cat_file = tmp_path / "mcp_catalog.json"
    cat_file.write_text(json.dumps({
        "mytool": {"description": "My reviewed tool", "transport": "sse",
                   "url": "https://mcp.example.com/sse", "trust": "community"},
    }))
    monkeypatch.setenv("MCP_INSTALL_CATALOG_FILE", str(cat_file))
    from tools.mcp.catalog import MCPCatalog

    cat = MCPCatalog()
    assert cat.is_allowed("mytool")
    entry = cat.get("mytool")
    assert entry is not None and entry.url == "https://mcp.example.com/sse"
    # builtins still present
    assert cat.get("github") is not None


# ------------------------------------------------------------------ refusals

def test_forged_turn_cannot_install(monkeypatch, log):
    monkeypatch.setenv("APPROVAL_PROVIDER", "auto")
    c, sm, _ = _controller(monkeypatch)
    res = _run(c, {"action": "install", "server_id": "github"},
               ctx=_ctx(is_sub_agent=True, role="leaf"))
    assert sm.added == []
    assert "owner" in (res.extracted_content or res.error or "").lower()


def test_deny_by_default_without_approval_provider(monkeypatch, log):
    monkeypatch.delenv("APPROVAL_PROVIDER", raising=False)
    c, sm, _ = _controller(monkeypatch)
    res = _run(c, {"action": "install", "server_id": "github"})
    assert sm.added == []
    txt = (res.extracted_content or res.error or "").lower()
    assert "not approved" in txt or "approv" in txt


# ------------------------------------------------------------------ happy path

def test_owner_install_with_auto_approver(monkeypatch, log):
    monkeypatch.setenv("APPROVAL_PROVIDER", "auto")
    ums = _FakeUMS()
    c, sm, rereg = _controller(monkeypatch, ums=ums)
    res = _run(c, {"action": "install", "server_id": "github"})
    assert res.error is None
    assert sm.added == ["github"]
    # T3-05: direct actions re-registered same-session
    assert len(rereg) == 1
    # T3-04: persisted tenant-scoped (github entry is url-based)
    assert ums.persisted and ums.persisted[0][0] == "u1"
    # audit row
    rows = log.query(kind="mcp_install")
    assert rows and rows[0]["attrs"]["server_id"] == "github"
    assert rows[0]["attrs"]["outcome"] == "installed"


def test_list_shows_catalog(monkeypatch):
    c, _, _ = _controller(monkeypatch)
    res = _run(c, {"action": "list"})
    assert "github" in (res.extracted_content or "")


def test_install_requires_loaded_mcp_tool(monkeypatch):
    monkeypatch.setenv("APPROVAL_PROVIDER", "auto")
    c, _, _ = _controller(monkeypatch, with_mcp_tool=False)
    res = _run(c, {"action": "install", "server_id": "github"})
    assert "mcp" in (res.error or res.extracted_content or "").lower()
