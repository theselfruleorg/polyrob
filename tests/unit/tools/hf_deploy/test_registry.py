"""deployed_apps registry (proposal §3.5): WAL store, tenant-scoped, caps."""
import time

import pytest


def _reg(tmp_path):
    from tools.hf_deploy.registry import DeployedAppsRegistry
    return DeployedAppsRegistry(str(tmp_path / "deployed_apps.db"))


def test_schema_creates_and_get_returns_none_for_unknown(tmp_path):
    reg = _reg(tmp_path)
    assert reg.get("app-a", "u1") is None


def test_upsert_pending_and_get_roundtrip(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert_pending("app-a", "u1", space_repo="org/app-a")
    row = reg.get("app-a", "u1")
    assert row["status"] == "pending"
    assert row["space_repo"] == "org/app-a"
    assert row["approved_at"] is None
    assert row["created_at"]


def test_upsert_pending_never_clobbers_existing_row(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert_pending("app-a", "u1", space_repo="org/app-a")
    reg.mark_approved("app-a", "u1")
    reg.upsert_pending("app-a", "u1", space_repo="org/other")
    row = reg.get("app-a", "u1")
    assert row["approved_at"] is not None  # approval survived
    assert row["space_repo"] == "org/app-a"


def test_tenant_scoping_same_app_name_isolated(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert_pending("app-a", "u1")
    reg.mark_approved("app-a", "u1")
    assert reg.get("app-a", "u2") is None
    reg.upsert_pending("app-a", "u2")
    assert reg.get("app-a", "u2")["approved_at"] is None
    assert reg.list_for("u2")[0]["user_id"] == "u2"
    assert len(reg.list_for("u1")) == 1


def test_record_live_sets_status_url_digest_and_last_deploy(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert_pending("app-a", "u1")
    reg.record_live("app-a", "u1", space_repo="org/app-a",
                    public_url="https://org-app-a.hf.space",
                    health_path="/health", workspace_digest="d" * 64)
    row = reg.get("app-a", "u1")
    assert row["status"] == "live"
    assert row["public_url"] == "https://org-app-a.hf.space"
    assert row["health_path"] == "/health"
    assert row["workspace_digest"] == "d" * 64
    assert row["last_deploy"]


def test_record_failed_and_set_status(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert_pending("app-a", "u1")
    reg.record_failed("app-a", "u1")
    assert reg.get("app-a", "u1")["status"] == "failed"
    reg.set_status("app-a", "u1", "undeployed")
    assert reg.get("app-a", "u1")["status"] == "undeployed"


def test_set_status_rejects_unknown_status(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert_pending("app-a", "u1")
    with pytest.raises(ValueError):
        reg.set_status("app-a", "u1", "totally-bogus")


def test_deploy_attempt_counting_is_tenant_scoped(tmp_path):
    reg = _reg(tmp_path)
    for _ in range(3):
        reg.record_attempt("app-a", "u1")
    reg.record_attempt("app-b", "u2")
    assert reg.deploys_in_last_day("u1") == 3
    assert reg.deploys_in_last_day("u2") == 1


def test_last_attempt_epoch_per_app(tmp_path):
    reg = _reg(tmp_path)
    assert reg.last_attempt_epoch("app-a", "u1") is None
    before = time.time()
    reg.record_attempt("app-a", "u1")
    got = reg.last_attempt_epoch("app-a", "u1")
    assert got is not None and got >= before - 1
    # another tenant's attempt never bleeds in
    assert reg.last_attempt_epoch("app-a", "u2") is None


def test_list_live_all_crosses_tenants_for_boot_reconcile(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert_pending("app-a", "u1")
    reg.record_live("app-a", "u1", space_repo="o/a", public_url="https://x",
                    health_path="/", workspace_digest="d")
    reg.upsert_pending("app-b", "u2")
    reg.record_live("app-b", "u2", space_repo="o/b", public_url="https://y",
                    health_path="/", workspace_digest="d")
    reg.upsert_pending("app-c", "u1")  # pending — not swept
    live = reg.list_live_all()
    assert {(r["app_name"], r["user_id"]) for r in live} == {("app-a", "u1"), ("app-b", "u2")}


def test_default_db_path_env_override_wins(tmp_path, monkeypatch):
    from tools.hf_deploy.registry import default_deployed_apps_db
    monkeypatch.setenv("DEPLOYED_APPS_DB_PATH", str(tmp_path / "redirected.db"))
    assert default_deployed_apps_db() == str(tmp_path / "redirected.db")
