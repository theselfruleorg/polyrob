"""032 — AppSupervisor.tick with fake docker/system runners."""
import asyncio
import os
import types

import pytest

from core.app_service.egress import EgressApplier
from core.app_service.nginx import NginxApplier
from core.app_service.registry import AppServiceRegistry
from core.app_service.supervisor import AppSupervisor


class FakeDocker:
    def __init__(self):
        self.log = []
        self.running = "true"
        self.fail_run = False

    async def __call__(self, args, *, input=None, timeout=None):
        self.log.append(list(args))
        head = args[:2]
        if head == ["run", "-d"]:
            return (1, "", "boom") if self.fail_run else (0, "cid123\n", "")
        if head == ["network", "inspect"]:
            return (0, "172.30.7.0/24\n", "")
        if args[:1] == ["inspect"]:
            return (0, f"{self.running}\n", "")
        if args[:1] == ["logs"]:
            return (0, "hello from app\n", "")
        return (0, "", "")

    def argv_of(self, verb):
        return [a for a in self.log if a[:len(verb)] == verb]


class FakeSys:
    """nft/nginx runner. Models the kernel losing an nft table (reboot, flush)."""

    def __init__(self):
        self.log = []
        self.missing_tables = set()
        self.fail_apply = False

    async def __call__(self, argv, *, input=None, timeout=None):
        self.log.append((list(argv), input))
        if argv[:3] == ["nft", "list", "table"]:
            if argv[-1] in self.missing_tables:
                return (1, "", "Error: No such file or directory")
            return (0, "", "")
        if argv[:3] == ["nft", "-f", "-"]:
            if self.fail_apply:
                return (1, "", "nft: permission denied")
            self.missing_tables.clear()  # the rules are loaded again
        return (0, "", "")


class FakeEvents:
    def __init__(self):
        self.rows = []

    def record(self, kind, **kw):
        self.rows.append((kind, kw))


def _allow():
    return types.SimpleNamespace(allowed=True, reason="")


def _deny():
    return types.SimpleNamespace(allowed=False, reason="paused (apps) by owner via test")


@pytest.fixture
def rig(tmp_path):
    reg = AppServiceRegistry(str(tmp_path / "a.db"))
    proj = tmp_path / "data" / "project" / "rob-status"
    proj.mkdir(parents=True)
    (proj / "server.py").write_text("print('hi')\n")
    (proj / ".env").write_text("SECRET=1\n")
    reg.upsert_request("rob-status", "owner-1", source_dir=str(proj), cmd=["python", "server.py"],
                       container_port=8765, health_path="/api/status.json", egress="none",
                       egress_allow=[], env={"LOG_LEVEL": "info"}, workspace_digest="d" * 64)
    reg.mark_approved("rob-status", "owner-1")
    docker, sys_r, events = FakeDocker(), FakeSys(), FakeEvents()
    state = {"healthy": True, "allow": _allow}

    async def probe(url, timeout):
        state["probes"] = state.get("probes", []) + [url]
        return state["healthy"]

    sup = AppSupervisor(
        reg, data_dir=str(tmp_path / "data"), base_domain="apps.example.test", allow_public=True,
        docker_runner=docker, sys_runner=sys_r, nginx=NginxApplier(str(tmp_path / "apps.d"), sys_r),
        egress=EgressApplier(sys_r), image="polyrob-dev:test", memory_mb=512, cpus=0.5, pids=256,
        snapshot_max_mb=50, port_range=(18000, 18002), health_timeout_sec=1,
        cert_dir="/etc/letsencrypt/live/apps.example.test", source_roots=[str(tmp_path / "data")],
        allows_fn=lambda kind: state["allow"](), http_probe=probe, poll_sec=0, event_log=events,
    )
    return types.SimpleNamespace(reg=reg, sup=sup, docker=docker, sys=sys_r, events=events,
                                 state=state, tmp=tmp_path, proj=proj)


def requeue(rig, **over):
    """Queue another deploy of the rig's app.

    Approval binds to the approved CONFIG, so a request that moves a
    security-relevant field returns the row to ``pending``; these tests are
    about the supervisor, so the owner decision is replayed here."""
    kw = dict(source_dir=str(rig.proj), cmd=["python", "server.py"], container_port=8765,
              health_path="/api/status.json", egress="none", egress_allow=[],
              env={"LOG_LEVEL": "info"}, workspace_digest="e" * 64)
    kw.update(over)
    rig.reg.upsert_request("rob-status", "owner-1", **kw)
    if rig.reg.get("rob-status", "owner-1")["status"] == "pending":
        rig.reg.mark_approved("rob-status", "owner-1")


def test_deploy_happy_path(rig):
    counts = asyncio.run(rig.sup.tick())
    assert counts["deployed"] == 1 and counts["failed"] == 0
    row = rig.reg.get("rob-status", "owner-1")
    assert row["status"] == "live" and row["host_port"] == 18000
    assert row["public_url"] == "https://rob-status.apps.example.test"
    assert row["container_name"] == "polyrob-app-owner-1-rob-status"
    run = rig.docker.argv_of(["run", "-d"])[0]
    assert "--restart" in run and run[run.index("--restart") + 1] == "unless-stopped"
    assert "-p" in run and run[run.index("-p") + 1] == "127.0.0.1:18000:8765"
    assert "--cap-drop" in run and "--read-only" in run and "no-new-privileges" in run
    mount = run[run.index("-v") + 1]
    assert mount.endswith("/snapshots/dddddddddddd:/app:ro")
    assert run[run.index("--network") + 1] == "polyrob-app-owner-1-rob-status"
    assert run[-3:] == ["polyrob-dev:test", "python", "server.py"]
    envs = [run[i + 1] for i, a in enumerate(run) if a == "-e"]
    assert envs == ["LOG_LEVEL=info", "PORT=8765"]
    assert not any(k in " ".join(run) for k in ("API_KEY", "TOKEN", "SECRET"))
    # snapshot refused the credential file
    snap = os.path.dirname(mount.split(":")[0])
    assert os.path.exists(os.path.join(mount.split(":")[0], "server.py"))
    assert not os.path.exists(os.path.join(mount.split(":")[0], ".env"))
    # egress rules applied BEFORE run, stanza after health
    ops = [a for a, _ in rig.sys.log]
    assert ops.index(["nft", "-f", "-"]) < len(rig.docker.log)
    assert ["nginx", "-t"] in ops and ["systemctl", "reload", "nginx"] in ops
    stanza = (rig.tmp / "apps.d" / "rob-status.conf").read_text()
    assert "server_name rob-status.apps.example.test;" in stanza
    assert "proxy_pass http://127.0.0.1:18000/;" in stanza
    assert rig.state["probes"][0] == "http://127.0.0.1:18000/api/status.json"
    kinds = [k for k, _ in rig.events.rows]
    assert kinds == ["app_live"]
    assert rig.events.rows[0][1]["attrs"]["url"] == "https://rob-status.apps.example.test"
    # 021: the app dir carries the URL in the artifact ledger
    from core.artifacts import get_artifact_ledger
    art = get_artifact_ledger()._row_by_path("owner-1", os.path.realpath(str(rig.proj / "server.py")))
    assert art and art["url"] == "https://rob-status.apps.example.test"


def test_health_fail_is_failed_and_torn_down(rig):
    rig.state["healthy"] = False
    counts = asyncio.run(rig.sup.tick())
    assert counts["failed"] == 1
    row = rig.reg.get("rob-status", "owner-1")
    assert row["status"] == "failed" and "health check failed" in row["last_failure_error"]
    assert row["consecutive_failures"] == 1
    assert not (rig.tmp / "apps.d" / "rob-status.conf").exists()
    assert ["rm", "-f", "polyrob-app-owner-1-rob-status"] in rig.docker.log
    assert rig.events.rows[-1][0] == "app_failed"
    # a redeploy request resets it and the next tick tries again
    rig.state["healthy"] = True
    requeue(rig, workspace_digest="e" * 64)
    counts = asyncio.run(rig.sup.tick())
    assert counts["deployed"] == 1 and rig.reg.get("rob-status", "owner-1")["status"] == "live"


def test_docker_run_failure_records_error(rig):
    rig.docker.fail_run = True
    asyncio.run(rig.sup.tick())
    row = rig.reg.get("rob-status", "owner-1")
    assert row["status"] == "failed" and "docker run failed" in row["last_failure_error"]


def test_pause_edge_stops_live_apps_and_resume_redeploys(rig):
    asyncio.run(rig.sup.tick())
    rig.state["allow"] = _deny
    counts = asyncio.run(rig.sup.tick())
    assert counts["paused"] == 1
    row = rig.reg.get("rob-status", "owner-1")
    assert row["status"] == "paused" and "paused" in row["last_failure_error"]
    assert not (rig.tmp / "apps.d" / "rob-status.conf").exists()
    assert rig.events.rows[-1][0] == "app_stopped" and rig.events.rows[-1][1]["attrs"]["by"] == "pause"
    rig.state["allow"] = _allow
    counts = asyncio.run(rig.sup.tick())
    assert counts["resumed"] == 1 and counts["deployed"] == 1
    assert rig.reg.get("rob-status", "owner-1")["status"] == "live"


def test_stop_cleans_up(rig):
    asyncio.run(rig.sup.tick())
    rig.reg.set_status("rob-status", "owner-1", "stopped", error="owner kill")
    counts = asyncio.run(rig.sup.tick())
    assert counts["stopped"] == 1
    row = rig.reg.get("rob-status", "owner-1")
    assert row["status"] == "stopped" and row["container_name"] is None and row["host_port"] is None
    assert not (rig.tmp / "apps.d" / "rob-status.conf").exists()
    assert ["network", "rm", "polyrob-app-owner-1-rob-status"] in rig.docker.log


def test_loopback_only_when_public_is_off(rig):
    rig.sup.allow_public = False
    asyncio.run(rig.sup.tick())
    row = rig.reg.get("rob-status", "owner-1")
    assert row["status"] == "live" and row["public_url"] == "http://127.0.0.1:18000"
    assert not (rig.tmp / "apps.d").exists() or not list((rig.tmp / "apps.d").iterdir())


def test_live_check_container_gone_and_unhealthy_ticks(rig):
    asyncio.run(rig.sup.tick())
    rig.state["healthy"] = False
    for _ in range(2):
        counts = asyncio.run(rig.sup.tick())
        assert counts["unhealthy"] == 1
        assert rig.reg.get("rob-status", "owner-1")["status"] == "live"
    asyncio.run(rig.sup.tick())  # third consecutive failure opens the breaker
    row = rig.reg.get("rob-status", "owner-1")
    assert row["status"] == "failed" and "3 consecutive" in row["last_failure_error"]
    # logs were refreshed while live
    from core.app_service.config import logs_path
    assert "hello from app" in open(logs_path(str(rig.tmp / "data"), "owner-1", "rob-status")).read()
    # container vanished (e.g. after a reboot without --restart) -> failed
    rig.state["healthy"] = True
    requeue(rig, workspace_digest="f" * 64)
    asyncio.run(rig.sup.tick())
    rig.docker.running = "false"
    asyncio.run(rig.sup.tick())
    assert rig.reg.get("rob-status", "owner-1")["last_failure_error"] == "container not running"


def test_source_outside_roots_and_port_exhaustion(rig):
    rig.sup.source_roots = [str(rig.tmp / "elsewhere")]
    asyncio.run(rig.sup.tick())
    row = rig.reg.get("rob-status", "owner-1")
    assert row["status"] == "failed" and "outside the allowed source roots" in row["last_failure_error"]
    rig.sup.source_roots = [str(rig.tmp / "data")]
    rig.sup.port_range = (18000, 18000)
    for i in range(1):
        rig.reg.upsert_request(f"other{i}", "owner-1", source_dir=str(rig.proj), cmd=["x"],
                               container_port=80, health_path="/", egress="none", egress_allow=[],
                               env={}, workspace_digest="a" * 64)
        rig.reg.mark_approved(f"other{i}", "owner-1")
        rig.reg.record_live(f"other{i}", "owner-1", host_port=18000, container_name="c", public_url="u")
    requeue(rig, workspace_digest="b" * 64)
    asyncio.run(rig.sup.tick())
    assert "exhausted" in rig.reg.get("rob-status", "owner-1")["last_failure_error"]


def test_allowlist_egress_resolves_and_renders(rig):
    requeue(rig, egress="allowlist", egress_allow=["api.example.com"],
            workspace_digest="c" * 64)
    rig.sup._resolver = lambda h: ["93.184.216.34"]
    asyncio.run(rig.sup.tick())
    nft = [inp for a, inp in rig.sys.log if a == ["nft", "-f", "-"]][0]
    assert "ip saddr 172.30.7.0/24 ip daddr { 93.184.216.34 } accept" in nft


def test_cli_supervise_once_and_flag_off(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from cli.commands import apps as mod
    calls = []

    class _Sup:
        async def tick(self):
            calls.append(1)
            return {"deployed": 0}
    monkeypatch.setattr(mod, "build_supervisor", lambda: _Sup())
    monkeypatch.setenv("APP_SERVICE_ENABLED", "true")
    res = CliRunner().invoke(mod.apps, ["supervise", "--once"])
    assert res.exit_code == 0, res.output
    assert calls == [1]
    monkeypatch.delenv("APP_SERVICE_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_BUILDER_MODE", raising=False)
    res = CliRunner().invoke(mod.apps, ["supervise", "--once"])
    assert res.exit_code != 0 and "APP_SERVICE_ENABLED" in res.output
    from cli.polyrob import _LAZY_SUBCOMMANDS
    assert _LAZY_SUBCOMMANDS["apps"] == "cli.commands.apps:apps"


# --- egress is RE-ASSERTED every tick, not only at deploy --------------------

def test_live_tick_reapplies_lost_egress_rules(rig):
    """nft rules are ephemeral kernel state and the container carries
    `--restart unless-stopped`: after a reboot the app is back with NO egress
    restriction, and nothing used to re-apply it."""
    asyncio.run(rig.sup.tick())
    nft_applies = [a for a, _ in rig.sys.log if a == ["nft", "-f", "-"]]
    assert len(nft_applies) == 1
    # the table survives -> the cheap presence check keeps the tick free
    asyncio.run(rig.sup.tick())
    assert ["nft", "list", "table", "inet", "polyrob_app_rob_status"] in [a for a, _ in rig.sys.log]
    assert len([a for a, _ in rig.sys.log if a == ["nft", "-f", "-"]]) == 1
    # the kernel lost the table (reboot / firewall flush) -> re-applied
    rig.sys.missing_tables.add("polyrob_app_rob_status")
    counts = asyncio.run(rig.sup.tick())
    assert counts["healthy"] == 1
    scripts = [inp for a, inp in rig.sys.log if a == ["nft", "-f", "-"]]
    assert len(scripts) == 2 and "ip saddr 172.30.7.0/24 drop" in scripts[-1]
    assert rig.reg.get("rob-status", "owner-1")["status"] == "live"


def test_unprotected_app_is_never_reported_healthy(rig):
    asyncio.run(rig.sup.tick())
    rig.sys.missing_tables.add("polyrob_app_rob_status")
    rig.sys.fail_apply = True
    for i in (1, 2):
        counts = asyncio.run(rig.sup.tick())
        assert counts["unhealthy"] == 1 and counts["healthy"] == 0
        row = rig.reg.get("rob-status", "owner-1")
        assert row["status"] == "live" and row["consecutive_failures"] == i
        assert "egress rules not asserted" in row["last_failure_error"]
        assert rig.events.rows[-1][0] == "app_failed"
    asyncio.run(rig.sup.tick())  # third bad tick opens the breaker
    row = rig.reg.get("rob-status", "owner-1")
    assert row["status"] == "failed" and "egress rules not asserted" in row["last_failure_error"]
    assert ["rm", "-f", "polyrob-app-owner-1-rob-status"] in rig.docker.log


def test_open_egress_needs_no_table(rig):
    requeue(rig, egress="open", workspace_digest="c" * 64)
    asyncio.run(rig.sup.tick())
    assert rig.reg.get("rob-status", "owner-1")["status"] == "live"
    rig.sys.log.clear()
    counts = asyncio.run(rig.sup.tick())
    assert counts["healthy"] == 1
    assert not [a for a, _ in rig.sys.log if a[:1] == ["nft"]]


def test_a_live_app_is_never_silently_reconfigured(rig):
    """A redeploy that moves a security-relevant field while the app is LIVE is
    REFUSED: writing it onto the row would leave the row and the running
    container disagreeing (and would release the address). Stop, then request."""
    import pytest as _pytest
    asyncio.run(rig.sup.tick())
    assert rig.reg.get("rob-status", "owner-1")["status"] == "live"
    with _pytest.raises(ValueError) as e:
        rig.reg.upsert_request("rob-status", "owner-1", source_dir=str(rig.proj),
                               cmd=["python", "exfiltrate.py"], container_port=8765,
                               health_path="/api/status.json", egress="open", egress_allow=[],
                               env={"LOG_LEVEL": "info"}, workspace_digest="a" * 64)
    assert "app_stop rob-status" in str(e.value)
    assert "the start command and the egress mode changed" in str(e.value)
    row = rig.reg.get("rob-status", "owner-1")
    assert row["status"] == "live" and row["cmd"] == ["python", "server.py"]
    assert row["egress"] == "none" and rig.reg.slug_holder("rob-status") == "owner-1"
    # the honest route: stop, request the new configuration, owner approves
    rig.reg.set_status("rob-status", "owner-1", "stopped", error="agent stop")
    asyncio.run(rig.sup.tick())
    new = rig.reg.upsert_request("rob-status", "owner-1", source_dir=str(rig.proj),
                                 cmd=["python", "server.py"], container_port=8765,
                                 health_path="/api/status.json", egress="open", egress_allow=[],
                                 env={"LOG_LEVEL": "info"}, workspace_digest="a" * 64)
    assert new["status"] == "pending" and new["approval_change"] == ["egress"]
    assert rig.reg.mark_approved("rob-status", "owner-1")
    assert asyncio.run(rig.sup.tick())["deployed"] == 1
