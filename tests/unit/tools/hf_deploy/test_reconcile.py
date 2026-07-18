"""Reconcile-on-boot (§3.5): re-health-check `live` rows; drift flips status —
fail-open on unreachable checks; never touches non-live rows."""
import asyncio


def _seed(tmp_path):
    from tools.hf_deploy.registry import DeployedAppsRegistry
    reg = DeployedAppsRegistry(str(tmp_path / "deployed_apps.db"))
    reg.upsert_pending("app-good", "u1")
    reg.record_live("app-good", "u1", space_repo="o/app-good",
                    public_url="https://o-app-good.hf.space", health_path="/",
                    workspace_digest="d")
    reg.upsert_pending("app-dead", "u1")
    reg.record_live("app-dead", "u1", space_repo="o/app-dead",
                    public_url="https://o-app-dead.hf.space", health_path="/health",
                    workspace_digest="d")
    reg.upsert_pending("app-pending", "u1")
    return reg


def test_reconcile_flips_drifted_live_rows(tmp_path):
    from tools.hf_deploy.reconcile import reconcile_deployed_apps
    reg = _seed(tmp_path)

    def getter(url, timeout):
        return 200 if "app-good" in url else 404

    flipped = asyncio.run(reconcile_deployed_apps(
        db_path=reg.db_path, http_get=getter))
    assert flipped == 1
    assert reg.get("app-good", "u1")["status"] == "live"
    assert reg.get("app-dead", "u1")["status"] == "failed"
    assert reg.get("app-pending", "u1")["status"] == "pending"


def test_reconcile_checks_health_path(tmp_path):
    from tools.hf_deploy.reconcile import reconcile_deployed_apps
    reg = _seed(tmp_path)
    seen = []

    def getter(url, timeout):
        seen.append(url)
        return 200

    asyncio.run(reconcile_deployed_apps(db_path=reg.db_path, http_get=getter))
    assert "https://o-app-dead.hf.space/health" in seen
    assert "https://o-app-good.hf.space/" in seen


def test_reconcile_fails_open_on_getter_error(tmp_path):
    from tools.hf_deploy.reconcile import reconcile_deployed_apps
    reg = _seed(tmp_path)

    def getter(url, timeout):
        raise RuntimeError("dns down")

    flipped = asyncio.run(reconcile_deployed_apps(db_path=reg.db_path, http_get=getter))
    assert flipped == 0
    assert reg.get("app-good", "u1")["status"] == "live"
    assert reg.get("app-dead", "u1")["status"] == "live"


def test_autonomy_runtime_schedules_reconcile_only_when_enabled(monkeypatch):
    import core.autonomy_runtime as rt
    import tools.hf_deploy.reconcile as rec

    calls = []

    async def fake_reconcile(*a, **k):
        calls.append((a, k))
        return 0

    monkeypatch.setattr(rec, "reconcile_deployed_apps", fake_reconcile)

    async def drive():
        rt._schedule_hf_deploy_reconcile()
        await asyncio.sleep(0.05)

    monkeypatch.delenv("HF_DEPLOY_ENABLED", raising=False)
    asyncio.run(drive())
    assert calls == []

    monkeypatch.setenv("HF_DEPLOY_ENABLED", "true")
    asyncio.run(drive())
    assert len(calls) == 1
