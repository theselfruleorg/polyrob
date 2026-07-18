"""HFDeployTool.deploy — happy path, health-fail BLOCKED semantics, green-test
gate, token custody (never in a result or a log line)."""
import asyncio
import logging

import pytest


class FakeBroker:
    def __init__(self, url="https://test-org-app-x.hf.space", healthy=True, fail=None):
        self.url, self.healthy, self.fail = url, healthy, fail
        self.deployed, self.checked, self.deleted = [], [], []

    async def deploy_space(self, *, space_repo, workspace_dir, secrets=None):
        if self.fail is not None:
            from tools.hf_deploy.broker import BrokerError
            raise BrokerError(self.fail)
        self.deployed.append((space_repo, workspace_dir, secrets))
        return self.url

    async def health_check(self, url, timeout=10.0):
        self.checked.append(url)
        return self.healthy

    async def delete_space(self, *, space_repo):
        self.deleted.append(space_repo)


class Approver:
    def __init__(self, allow=True):
        self.allow, self.requests = allow, []

    async def request(self, action_name, params, context):
        self.requests.append((action_name, dict(params or {})))
        return self.allow


def make_tool(tmp_path, *, broker=None, approver=None, orch="green"):
    from tools.hf_deploy.tool import HFDeployTool
    from tools.hf_deploy.registry import DeployedAppsRegistry
    t = HFDeployTool()
    t._registry = DeployedAppsRegistry(str(tmp_path / "deployed_apps.db"))
    t._broker = broker if broker is not None else FakeBroker()
    t._approval_provider = approver if approver is not None else Approver(True)
    t._orch = orch
    t._orchestrator_resolver = lambda sid: t._orch
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "app.py").write_text("print('hi')\n")
    t._workspace_root_override = str(ws)
    return t


def run_deploy(tool, ctx, **over):
    from tools.hf_deploy.tool import DeployParams
    kw = {"app_name": "app-x"}
    kw.update(over)
    return asyncio.run(tool.deploy(DeployParams(**kw), execution_context=ctx))


def test_deploy_happy_path_returns_url_and_records_live(
        tmp_path, owner_ctx, deploy_env, green_orch):
    tool = make_tool(tmp_path, orch=green_orch)
    res = run_deploy(tool, owner_ctx)
    assert not res.error, res.error
    assert "https://test-org-app-x.hf.space" in (res.extracted_content or "")
    row = tool._registry.get("app-x", "owner-1")
    assert row["status"] == "live"
    assert row["public_url"] == "https://test-org-app-x.hf.space"
    assert len(row["workspace_digest"]) == 64
    assert row["approved_at"] is not None
    # attempt counted against the daily cap
    assert tool._registry.deploys_in_last_day("owner-1") == 1


def test_deploy_health_check_fail_is_blocked_never_live(
        tmp_path, owner_ctx, deploy_env, green_orch):
    tool = make_tool(tmp_path, broker=FakeBroker(healthy=False), orch=green_orch)
    res = run_deploy(tool, owner_ctx)
    assert res.error and "2xx" in res.error
    row = tool._registry.get("app-x", "owner-1")
    assert row["status"] == "failed"
    assert row["status"] != "live"


def test_deploy_broker_failure_records_failed(
        tmp_path, owner_ctx, deploy_env, green_orch):
    tool = make_tool(tmp_path, broker=FakeBroker(fail="build exploded"), orch=green_orch)
    res = run_deploy(tool, owner_ctx)
    assert res.error and "build exploded" in res.error
    assert tool._registry.get("app-x", "owner-1")["status"] == "failed"


def test_deploy_health_check_exception_is_blocked(
        tmp_path, owner_ctx, deploy_env, green_orch):
    class BoomBroker(FakeBroker):
        async def health_check(self, url, timeout=10.0):
            raise RuntimeError("network down")

    tool = make_tool(tmp_path, broker=BoomBroker(), orch=green_orch)
    res = run_deploy(tool, owner_ctx)
    assert res.error
    assert tool._registry.get("app-x", "owner-1")["status"] == "failed"


def test_deploy_refuses_without_green_run_tests(
        tmp_path, owner_ctx, deploy_env, no_green_orch):
    tool = make_tool(tmp_path, orch=no_green_orch)
    res = run_deploy(tool, owner_ctx)
    assert res.error and "run_tests" in res.error
    assert tool._broker.deployed == []


def test_deploy_refuses_when_edited_after_green_tests(
        tmp_path, owner_ctx, deploy_env, edited_after_test_orch):
    tool = make_tool(tmp_path, orch=edited_after_test_orch)
    res = run_deploy(tool, owner_ctx)
    assert res.error and "edited" in res.error.lower()
    assert tool._broker.deployed == []


def test_deploy_refuses_closed_when_ledger_unreachable(
        tmp_path, owner_ctx, deploy_env):
    tool = make_tool(tmp_path, orch=None)
    res = run_deploy(tool, owner_ctx)
    assert res.error and "cannot verify" in res.error.lower()
    assert tool._broker.deployed == []


def test_deploy_refuses_when_workspace_unresolvable(
        tmp_path, owner_ctx, deploy_env, green_orch):
    # NEVER fall back to os.getcwd() (= the install tree on headless prod) for a
    # PUBLIC publish — an unresolvable workspace must refuse, broker untouched.
    tool = make_tool(tmp_path, orch=green_orch)
    tool._workspace_root_override = None
    tool._resolve_workspace_root = lambda ec: None  # simulate pm() resolution failure
    res = run_deploy(tool, owner_ctx)
    assert res.error and "workspace" in res.error.lower()
    assert tool._broker.deployed == []


def test_deploy_refuses_without_org(tmp_path, owner_ctx, monkeypatch, green_orch):
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    monkeypatch.delenv("HF_DEPLOY_ORG", raising=False)
    tool = make_tool(tmp_path, orch=green_orch)
    res = run_deploy(tool, owner_ctx)
    assert res.error and "HF_DEPLOY_ORG" in res.error


def test_deploy_refuses_without_token(tmp_path, owner_ctx, monkeypatch, green_orch):
    monkeypatch.setenv("HF_DEPLOY_ORG", "test-org")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    tool = make_tool(tmp_path, orch=green_orch)
    res = run_deploy(tool, owner_ctx)
    assert res.error and "HF_TOKEN" in res.error


def test_deploy_rejects_bad_app_names(tmp_path, owner_ctx, deploy_env, green_orch):
    tool = make_tool(tmp_path, orch=green_orch)
    for bad in ("../evil", "UPPER", "a", "x" * 80, "sp ace", "dot.dot"):
        res = run_deploy(tool, owner_ctx, app_name=bad)
        assert res.error, bad
    assert tool._broker.deployed == []


def test_token_never_in_results_or_logs(
        tmp_path, owner_ctx, deploy_env, green_orch, caplog):
    token = deploy_env
    caplog.set_level(logging.DEBUG)
    tool = make_tool(tmp_path, orch=green_orch)
    ok = run_deploy(tool, owner_ctx)
    tool2 = make_tool(tmp_path, broker=FakeBroker(fail="upload refused (401)"),
                      orch=green_orch)
    err = run_deploy(tool2, owner_ctx, app_name="app-y")
    for res in (ok, err):
        blob = f"{res.extracted_content}|{res.error}|{res.metadata}"
        assert token not in blob
    assert token not in caplog.text


def test_deploy_emits_audit_and_url_telemetry(
        tmp_path, owner_ctx, deploy_env, green_orch, monkeypatch):
    audits, events = [], []
    import agents.task.telemetry.self_events as se
    monkeypatch.setattr(se, "emit_self_modification",
                        lambda **kw: audits.append(kw))
    import tools.hf_deploy.tool as tool_mod
    monkeypatch.setattr(tool_mod, "_emit_event",
                        lambda kind, execution_context, attrs: events.append((kind, attrs)))
    tool = make_tool(tmp_path, orch=green_orch)
    res = run_deploy(tool, owner_ctx)
    assert not res.error
    assert any(a.get("kind") == "hf_deploy" and a.get("action") == "deploy"
               and a.get("ok") for a in audits)
    deployed = [e for e in events if e[0] == "app_deployed"]
    assert deployed and deployed[0][1]["url"] == "https://test-org-app-x.hf.space"


def test_undeploy_deletes_space_and_flips_status(
        tmp_path, owner_ctx, deploy_env, green_orch):
    from tools.hf_deploy.tool import UndeployParams
    tool = make_tool(tmp_path, orch=green_orch)
    assert not run_deploy(tool, owner_ctx).error
    res = asyncio.run(tool.undeploy(UndeployParams(app_name="app-x"),
                                    execution_context=owner_ctx))
    assert not res.error
    assert tool._broker.deleted == ["test-org/app-x"]
    assert tool._registry.get("app-x", "owner-1")["status"] == "undeployed"


def test_undeploy_unknown_app_refused(tmp_path, owner_ctx, deploy_env):
    from tools.hf_deploy.tool import UndeployParams
    tool = make_tool(tmp_path)
    res = asyncio.run(tool.undeploy(UndeployParams(app_name="ghost"),
                                    execution_context=owner_ctx))
    assert res.error and "unknown app" in res.error


def test_list_deployments_is_tenant_scoped(tmp_path, owner_ctx, deploy_env, green_orch):
    from tools.hf_deploy.tool import ListDeploymentsParams
    tool = make_tool(tmp_path, orch=green_orch)
    assert not run_deploy(tool, owner_ctx).error
    tool._registry.upsert_pending("other-app", "someone-else")
    res = asyncio.run(tool.list_deployments(ListDeploymentsParams(),
                                            execution_context=owner_ctx))
    assert not res.error
    assert "app-x" in res.extracted_content
    assert "other-app" not in res.extracted_content
