"""098 TS-30: the HF deploy registry follows what the Space does.

``deploying`` is on record before the publish; a health failure after a publish
is ``unhealthy`` and recovers on reconcile; every write is a CAS on the state
(and deploy) it read, so a probe of an old Space never overwrites a newer deploy."""
import asyncio
import time

from tools.hf_deploy.reconcile import reconcile_deployed_apps
from tools.hf_deploy.registry import STALE_DEPLOYING_SEC, DeployedAppsRegistry


def _reg(tmp_path):
    return DeployedAppsRegistry(str(tmp_path / "deployed_apps.db"))


def _live(reg, app="app-a", uid="u1"):
    assert reg.begin_deploy(app, uid, space_repo=f"o/{app}")
    assert reg.record_live(app, uid, space_repo=f"o/{app}", public_url=f"https://{app}",
                           health_path="/", workspace_digest="d")


def test_health_fail_after_publish_is_unhealthy_and_recovers_on_reconcile(tmp_path):
    reg = _reg(tmp_path)
    assert reg.begin_deploy("app-a", "u1")
    assert reg.record_unhealthy("app-a", "u1", error="cold start",
                                set_cols={"public_url": "https://app-a", "health_path": "/",
                                          "last_deploy": time.time()})
    assert reg.get("app-a", "u1")["status"] == "unhealthy"
    n = asyncio.run(reconcile_deployed_apps(reg.db_path, http_get=lambda u, t: 200))
    assert n == 1
    assert reg.get("app-a", "u1")["status"] == "live"


def test_reconcile_does_not_overwrite_newer_deploy(tmp_path, monkeypatch):
    """Reconcile reads row X live; a fresh deploy of X completes; the probe of
    the OLD Space then fails. The CAS (state + last_deploy) loses."""
    reg = _reg(tmp_path)
    _live(reg)
    stale = reg.list_reconcilable_all()
    time.sleep(0.01)
    _live(reg)  # the redeploy: live -> deploying -> live, new last_deploy
    monkeypatch.setattr(DeployedAppsRegistry, "list_reconcilable_all",
                        lambda self, **k: stale)
    n = asyncio.run(reconcile_deployed_apps(reg.db_path, http_get=lambda u, t: 503))
    assert n == 0
    assert reg.get("app-a", "u1")["status"] == "live"


def test_crash_mid_deploy_leaves_deploying_then_reconciled(tmp_path):
    reg = _reg(tmp_path)
    long_ago = time.time() - STALE_DEPLOYING_SEC - 5
    assert reg.begin_deploy("app-a", "u1", now=long_ago)
    # (crash: no record_live / record_failed)
    assert reg.get("app-a", "u1")["status"] == "deploying"
    n = asyncio.run(reconcile_deployed_apps(reg.db_path, http_get=lambda u, t: 200))
    assert n == 1
    assert reg.get("app-a", "u1")["status"] == "failed"
    # The next deploy retakes the app.
    assert reg.begin_deploy("app-a", "u1")


def test_crashed_redeploy_with_a_url_is_probed(tmp_path):
    reg = _reg(tmp_path)
    _live(reg)
    assert reg.begin_deploy("app-a", "u1", now=time.time() - STALE_DEPLOYING_SEC - 5)
    n = asyncio.run(reconcile_deployed_apps(reg.db_path, http_get=lambda u, t: 200))
    assert n == 1 and reg.get("app-a", "u1")["status"] == "live"


def test_a_second_deploy_waits_for_the_first(tmp_path):
    reg = _reg(tmp_path)
    assert reg.begin_deploy("app-a", "u1")
    assert reg.begin_deploy("app-a", "u1") is False


def test_record_live_needs_deploying(tmp_path):
    reg = _reg(tmp_path)
    _live(reg)
    reg.set_status("app-a", "u1", "undeployed")
    assert reg.record_live("app-a", "u1", space_repo="o", public_url="u", health_path="/",
                           workspace_digest="d") is False
    assert reg.get("app-a", "u1")["status"] == "undeployed"
