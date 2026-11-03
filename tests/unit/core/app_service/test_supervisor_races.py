"""098 CO-21: an owner kill is never overwritten by a racing deploy, health
check or pause."""
import asyncio

import pytest

from core.app_service import owner_ops
from core.event_kinds import APP_LIVE, APP_STOPPED
from tests.unit.core.app_service.test_app_supervisor import _deny, rig  # noqa: F401


def test_kill_during_deploy_stays_stopped_and_tears_down(rig):
    async def probe(url, timeout):
        # The owner kills the app while the deploy waits for health.
        owner_ops.kill(rig.reg, "demo-status", "owner-1", via="test")
        return True

    rig.sup._probe = probe
    counts = asyncio.run(rig.sup.tick())
    row = rig.reg.get("demo-status", "owner-1")
    assert row["status"] == "stopped"
    assert counts["deployed"] == 0
    assert ["network", "rm", "polyrob-app-owner-1-demo-status"] in rig.docker.log
    kinds = [k for k, _ in rig.events.rows]
    assert APP_STOPPED in kinds and APP_LIVE not in kinds


def test_kill_during_health_check_not_revived(rig):
    asyncio.run(rig.sup.tick())
    assert rig.reg.get("demo-status", "owner-1")["status"] == "live"
    row = rig.reg.get("demo-status", "owner-1")
    rig.sys.missing_tables.add("x")  # force reconcile_egress to work
    rig.sys.fail_apply = True

    async def table_present(slug):
        owner_ops.kill(rig.reg, "demo-status", "owner-1", via="test")
        return False

    rig.sup.egress.table_present = table_present
    asyncio.run(rig.sup.check_one(row))
    assert rig.reg.get("demo-status", "owner-1")["status"] == "stopped"


def test_pause_branch_does_not_overwrite_kill(rig):
    asyncio.run(rig.sup.tick())
    rig.state["allow"] = _deny
    orig = rig.sup._teardown

    async def teardown(row):
        owner_ops.kill(rig.reg, "demo-status", "owner-1", via="test")
        await orig(row)

    rig.sup._teardown = teardown
    counts = asyncio.run(rig.sup.tick())
    assert counts["paused"] == 0
    assert rig.reg.get("demo-status", "owner-1")["status"] == "stopped"


def test_request_during_deploy_refused(rig):
    rig.reg.claim("demo-status", "owner-1", "approved", "deploying")
    with pytest.raises(ValueError, match="deploying"):
        rig.reg.upsert_request("demo-status", "owner-1", source_dir=str(rig.proj),
                               cmd=["python", "server.py"], container_port=8765,
                               health_path="/api/status.json", egress="none", egress_allow=[],
                               env={"LOG_LEVEL": "info"}, workspace_digest="f" * 64)
    assert rig.reg.get("demo-status", "owner-1")["status"] == "deploying"
