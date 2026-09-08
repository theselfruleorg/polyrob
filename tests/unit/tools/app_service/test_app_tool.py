"""AppServiceTool — gates, validation, the pending-row ask, unattended redeploy, caps."""
import asyncio

import pytest

from core.app_service.registry import AppServiceRegistry


def make_tool(tmp_path, *, orch="green"):
    from tools.app_service.tool import AppServiceTool
    t = AppServiceTool()
    t._registry = AppServiceRegistry(str(tmp_path / "app_services.db"))
    t._orch = orch
    t._orchestrator_resolver = lambda sid: t._orch
    ws = tmp_path / "ws"
    (ws / "rob-status").mkdir(parents=True, exist_ok=True)
    (ws / "rob-status" / "server.py").write_text("print('hi')\n")
    t._workspace_override = str(ws)
    t._data_dir_override = str(tmp_path / "data")
    return t


def run_deploy(tool, ctx, **over):
    from tools.app_service.tool import DeployParams
    kw = {"slug": "rob-status", "dir": "rob-status", "cmd": ["python", "server.py"], "port": 8765}
    kw.update(over)
    return asyncio.run(tool.deploy(DeployParams(**kw), execution_context=ctx))


@pytest.fixture
def events(monkeypatch):
    seen = []
    monkeypatch.setattr("tools.app_service.tool._emit_event",
                        lambda kind, ctx, attrs: seen.append((kind, dict(attrs))))
    return seen


def test_new_slug_is_pending_and_names_the_approval_seats(tmp_path, owner_ctx, app_env,
                                                           green_orch, events):
    tool = make_tool(tmp_path, orch=green_orch)
    res = run_deploy(tool, owner_ctx)
    assert not res.error, res.error
    assert "PENDING owner approval" in res.extracted_content
    assert "polyrob apps approve rob-status" in res.extracted_content
    assert "/apps approve rob-status" in res.extracted_content
    assert "Do not report this as shipped" in res.extracted_content
    row = tool._registry.get("rob-status", "owner-1")
    assert row["status"] == "pending" and row["source_dir"].endswith("/ws/rob-status")
    assert len(row["workspace_digest"]) == 64
    assert events == [("app_requested", {"slug": "rob-status", "dir": "rob-status",
                                         "port": 8765, "egress": "none"})]
    assert tool._registry.deploys_in_last_day("owner-1") == 1


def test_approved_slug_redeploys_unattended(tmp_path, owner_ctx, app_env, green_orch, events,
                                            monkeypatch):
    """A CODE bump on the approved configuration is unattended — that is the
    documented behaviour the ship-software stream depends on. Only the workspace
    digest moves here; every security-relevant field is byte-identical."""
    monkeypatch.setenv("APP_SERVICE_MIN_INTERVAL_SEC", "0")
    tool = make_tool(tmp_path, orch=green_orch)
    run_deploy(tool, owner_ctx)
    first_digest = tool._registry.get("rob-status", "owner-1")["workspace_digest"]
    assert tool._registry.mark_approved("rob-status", "owner-1")
    tool._registry.record_live("rob-status", "owner-1", host_port=18000, container_name="c",
                               public_url="https://rob-status.apps.example.test")
    (tmp_path / "ws" / "rob-status" / "server.py").write_text("print('hi v2')\n")
    res = run_deploy(tool, owner_ctx)
    assert not res.error, res.error
    assert "Redeploy" in res.extracted_content and "approved address" in res.extracted_content
    row = tool._registry.get("rob-status", "owner-1")
    assert row["status"] == "approved" and row["workspace_digest"] != first_digest
    assert events[-1][0] == "app_approved" and events[-1][1]["redeploy"] is True


@pytest.mark.parametrize("over,field", [
    ({"cmd": ["python", "exfiltrate.py"]}, "the start command"),
    ({"egress": "open"}, "the egress mode"),
    ({"egress": "allowlist", "egress_allow": ["evil.example.com"]}, "the egress allow-list"),
    ({"env": {"LOG_LEVEL": "debug"}}, "the env variable names"),
    ({"port": 9999}, "the container port"),
    ({"health_path": "/other"}, "the health path"),
])
def test_changed_config_returns_to_pending(tmp_path, owner_ctx, app_env, green_orch, events,
                                           monkeypatch, over, field):
    """Approval binds to the approved CONFIG, not just the address: a redeploy
    that moves a security-relevant field needs a NEW owner decision and says
    WHICH field moved."""
    monkeypatch.setenv("APP_SERVICE_MIN_INTERVAL_SEC", "0")
    tool = make_tool(tmp_path, orch=green_orch)
    run_deploy(tool, owner_ctx)
    assert tool._registry.mark_approved("rob-status", "owner-1")
    res = run_deploy(tool, owner_ctx, **over)
    assert not res.error, res.error
    row = tool._registry.get("rob-status", "owner-1")
    assert row["status"] == "pending", over
    assert "NEW owner approval" in res.extracted_content
    assert field in res.extracted_content
    assert "polyrob apps approve rob-status" in res.extracted_content
    # reverting to exactly the approved configuration is unattended again
    res = run_deploy(tool, owner_ctx)
    assert tool._registry.get("rob-status", "owner-1")["status"] == "approved"
    assert "Redeploy" in res.extracted_content


@pytest.mark.parametrize("kind", ["leaf", "sub", "forged", "other_tenant"])
def test_provenance_denials(tmp_path, owner_ctx, app_env, green_orch, kind):
    from tools.controller.execution_context import ActionExecutionContext
    tool = make_tool(tmp_path, orch=green_orch)
    if kind == "leaf":
        ctx = ActionExecutionContext(session_id="s", user_id="owner-1", role="leaf")
    elif kind == "sub":
        ctx = ActionExecutionContext(session_id="s", user_id="owner-1", role="orchestrator",
                                     is_sub_agent=True)
    elif kind == "forged":
        ctx = ActionExecutionContext(session_id="s", user_id="owner-1", role="orchestrator",
                                     metadata={"turn_kind": "self_wake"})
    else:
        ctx = ActionExecutionContext(session_id="s", user_id="someone-else", role="orchestrator")
    res = run_deploy(tool, ctx)
    assert res.error and "denied" in res.error
    assert tool._registry.get("rob-status", "owner-1") is None
    assert tool._registry.get("rob-status", "someone-else") is None


def test_flag_off_and_pause_refuse(tmp_path, owner_ctx, green_orch, monkeypatch):
    tool = make_tool(tmp_path, orch=green_orch)
    monkeypatch.delenv("APP_SERVICE_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_BUILDER_MODE", raising=False)
    res = run_deploy(tool, owner_ctx)
    assert res.error and "APP_SERVICE_ENABLED" in res.error
    monkeypatch.setenv("APP_SERVICE_ENABLED", "true")
    from core import autonomy_control as ac
    home = tmp_path / "home"
    home.mkdir()
    # ONE base only — the record is written to every probed base.
    monkeypatch.delenv("DATA_ROOT", raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(home))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: home)
    ac.pause(str(home), scopes=("apps",), via="test")
    res = run_deploy(tool, owner_ctx)
    assert res.error and "paused" in res.error


@pytest.mark.parametrize("over,needle", [
    ({"slug": "Rob"}, "invalid slug"),
    ({"slug": "www"}, "reserved"),
    ({"egress": "everything"}, "egress must be"),
    ({"egress": "allowlist"}, "egress_allow"),
    ({"egress": "allowlist", "egress_allow": ["bad host"]}, "invalid hostname"),
    ({"dir": "../etc"}, "outside the workspace"),
    ({"dir": "missing"}, "not a directory"),
    ({"env": {"OPENAI_API_KEY": "x"}}, "secret-shaped"),
    ({"port": 0}, "1..65535"),
    ({"cmd": []}, "cmd must be"),
])
def test_validation(tmp_path, owner_ctx, app_env, green_orch, over, needle):
    tool = make_tool(tmp_path, orch=green_orch)
    res = run_deploy(tool, owner_ctx, **over)
    assert res.error and needle in res.error, res.error
    assert tool._registry.list_for("owner-1") == []


def test_ship_equals_tested(tmp_path, owner_ctx, app_env, no_green_orch):
    tool = make_tool(tmp_path, orch=no_green_orch)
    res = run_deploy(tool, owner_ctx)
    assert res.error and "run_tests" in res.error
    tool2 = make_tool(tmp_path, orch=None)
    res = run_deploy(tool2, owner_ctx)
    assert res.error and "cannot verify" in res.error


def test_caps(tmp_path, owner_ctx, app_env, green_orch, monkeypatch):
    monkeypatch.setenv("APP_SERVICE_MAX_LIVE", "1")
    monkeypatch.setenv("APP_SERVICE_MIN_INTERVAL_SEC", "0")
    tool = make_tool(tmp_path, orch=green_orch)
    (tmp_path / "ws" / "two").mkdir()
    run_deploy(tool, owner_ctx)
    tool._registry.mark_approved("rob-status", "owner-1")
    res = run_deploy(tool, owner_ctx, slug="two", dir="two")
    assert res.error and "APP_SERVICE_MAX_LIVE" in res.error
    monkeypatch.setenv("APP_SERVICE_MAX_LIVE", "3")
    monkeypatch.setenv("APP_SERVICE_MIN_INTERVAL_SEC", "3600")
    res = run_deploy(tool, owner_ctx)
    assert res.error and "MIN_INTERVAL" in res.error
    monkeypatch.setenv("APP_SERVICE_MIN_INTERVAL_SEC", "0")
    monkeypatch.setenv("APP_SERVICE_DAILY_MAX", "1")
    res = run_deploy(tool, owner_ctx, slug="two", dir="two")
    assert res.error and "DAILY_MAX" in res.error


def test_cross_tenant_slug_refused(tmp_path, owner_ctx, app_env, green_orch, monkeypatch):
    tool = make_tool(tmp_path, orch=green_orch)
    tool._registry.upsert_request("rob-status", "other", source_dir="x", cmd=["x"],
                                  container_port=1, health_path="/", egress="none",
                                  egress_allow=[], env={}, workspace_digest="d")
    tool._registry.mark_approved("rob-status", "other")
    res = run_deploy(tool, owner_ctx)
    assert res.error and "another tenant" in res.error


def test_stop_list_and_logs(tmp_path, owner_ctx, app_env, green_orch, events):
    from tools.app_service.tool import ListAppsParams, LogsParams, StopParams
    tool = make_tool(tmp_path, orch=green_orch)
    run_deploy(tool, owner_ctx)
    res = asyncio.run(tool.list_apps(ListAppsParams(), execution_context=owner_ctx))
    assert "rob-status [pending]" in res.extracted_content and "polyrob apps approve" in res.extracted_content
    res = asyncio.run(tool.logs(LogsParams(slug="rob-status"), execution_context=owner_ctx))
    assert "No logs yet" in res.extracted_content
    from core.app_service.config import logs_path
    import os
    p = logs_path(str(tmp_path / "data"), "owner-1", "rob-status")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write("".join(f"line {i}\n" for i in range(50)))
    res = asyncio.run(tool.logs(LogsParams(slug="rob-status", lines=3), execution_context=owner_ctx))
    assert "<untrusted_tool_result" in res.extracted_content and "line 49" in res.extracted_content
    assert "line 46" not in res.extracted_content
    res = asyncio.run(tool.stop(StopParams(slug="rob-status"), execution_context=owner_ctx))
    assert not res.error and tool._registry.get("rob-status", "owner-1")["status"] == "stopped"
    assert events[-1][0] == "app_stopped"
    res = asyncio.run(tool.stop(StopParams(slug="nope"), execution_context=owner_ctx))
    assert res.error


def test_a_live_app_is_not_reconfigured_by_a_redeploy(tmp_path, owner_ctx, app_env, green_orch,
                                                      monkeypatch):
    """The agent is told the honest route instead of silently rewriting a live
    public address (the P0: approved `hello-world`, redeployed with egress open)."""
    monkeypatch.setenv("APP_SERVICE_MIN_INTERVAL_SEC", "0")
    tool = make_tool(tmp_path, orch=green_orch)
    run_deploy(tool, owner_ctx)
    tool._registry.mark_approved("rob-status", "owner-1")
    tool._registry.record_live("rob-status", "owner-1", host_port=18000, container_name="c",
                               public_url="https://rob-status.apps.example.test")
    res = run_deploy(tool, owner_ctx, cmd=["python", "exfiltrate.py"], egress="open")
    assert res.error and "app_stop rob-status" in res.error
    row = tool._registry.get("rob-status", "owner-1")
    assert row["status"] == "live" and row["cmd"] == ["python", "server.py"]
    assert row["egress"] == "none"
