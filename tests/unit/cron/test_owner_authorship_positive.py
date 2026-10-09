"""Standing owner authority reads a POSITIVE ``authored_by == owner`` stamp
(verifier round 3, 2026-10-08).

Attack: an unstamped row counted as the owner's (``not is_agent_authored``), and
rows from before ``d2ef4acff`` may be the agent's — so the agent's own old job
carried the owner's X-post / room-moderation / rig authority. Natural flow:
every owner path stamps ``owner`` and keeps that authority; the one-time
``cron.stamp_authorship`` moves the old rows to the right side.
"""
import json
import sqlite3
from datetime import datetime

import pytest

from core.config_policy.rigs import is_owner_authored, resolve_rig_tools
from cron.jobs import CronJob, CronJobStore
from cron.service import CronService


def _svc(tmp_path):
    return CronService(CronJobStore(str(tmp_path / "cron.db")))


# ── the predicate and its readers ────────────────────────────────────────────

@pytest.mark.parametrize("payload,owner", [
    (None, False), ({}, False), ({"deliver": "telegram"}, False),
    ({"authored_by": "agent"}, False), ({"authored_by": "owner"}, True),
    ({"authored_by": " OWNER "}, True), ({"owner_granted": True}, True),
])
def test_owner_authority_is_a_positive_stamp(payload, owner):
    assert is_owner_authored(payload) is owner


def test_an_unstamped_rig_gets_the_agent_ceiling():
    ceiling = ["web_fetch"]
    assert resolve_rig_tools({"tools": ["web_fetch", "cronjob"]}, [], agent_ceiling=ceiling) \
        == ["web_fetch"]
    assert resolve_rig_tools({"tools": ["web_fetch", "cronjob"], "authored_by": "owner"}, [],
                             agent_ceiling=ceiling) == ["web_fetch", "cronjob"]


def test_an_unstamped_target_is_not_trusted():
    from core.wallet.buy_target import with_authorship
    target = {"chain": "base", "address": "0x" + "1" * 40}
    assert "authored_by" not in with_authorship(target, {})
    assert with_authorship(target, {"authored_by": "owner"})["authored_by"] == "owner"


def test_an_unstamped_job_may_not_run_a_write_verb(monkeypatch):
    from cron import write_job
    monkeypatch.setattr(write_job, "write_jobs_enabled", lambda: True)
    assert write_job.refusal({})[0] == "write_job_not_owner"
    assert write_job.refusal({"authored_by": "owner"}) is None


def test_the_runner_records_owner_standing_only_for_a_stamped_row():
    import inspect
    from cron import runner
    src = inspect.getsource(runner)
    assert "if is_owner_authored(payload):" in src
    assert "note_owner_job(job.id, job.task)" in src


# ── every owner path stamps ──────────────────────────────────────────────────

def test_owner_create_cron_stamps_owner(tmp_path):
    from core.owner_create import create_cron
    job = create_cron(_svc(tmp_path), task="moderate the den", schedule_spec="1h",
                      user_id="rob")
    assert job.payload["authored_by"] == "owner"


def test_cli_cron_schedule_and_digest_stamp_owner(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from cli.commands import cron as cron_cli
    svc = _svc(tmp_path)
    monkeypatch.setattr(cron_cli, "_service", lambda write=False: svc)
    monkeypatch.setattr(cron_cli, "_tenant", lambda user=None: "rob")
    monkeypatch.setattr(cron_cli, "_warn_if_cron_off", lambda: None)
    monkeypatch.setattr(cron_cli, "_warn_if_digest_off", lambda: None)
    r = CliRunner().invoke(cron_cli.cron, ["schedule", "summarise", "1h"])
    assert r.exit_code == 0, r.output
    r = CliRunner().invoke(cron_cli.cron, ["digest", "every day 08:00"])
    assert r.exit_code == 0, r.output
    assert {j.payload.get("authored_by") for j in svc.list_jobs(user_id="rob")} == {"owner"}


def test_operator_seeder_stamps_owner(tmp_path):
    from scripts._seed_cron import seed
    job = seed(str(tmp_path / "cron.db"), {"task": "digest", "schedule_spec": "1d",
                                            "user_id": "rob", "payload": {"digest": True}})
    assert job.payload["authored_by"] == "owner" and job.payload["digest"] is True


def test_cronjob_schedule_has_stamped_since_d2ef4acff():
    """The migration's cutoff rests on this: the agent's tool always stamps."""
    import inspect
    from tools import cronjob_tools
    src = inspect.getsource(cronjob_tools)
    assert "payload[AUTHORED_BY_KEY] = OWNER_AUTHOR if owner_turn else AGENT_AUTHOR" in src


# ── the one-time migration ───────────────────────────────────────────────────

def _row(store, jid, created, payload):
    store.add(CronJob(id=jid, task=f"task {jid}", schedule_spec="1h", user_id="rob",
                      next_run_at=None, payload=payload, created_at=created))


@pytest.fixture
def db(tmp_path):
    store = CronJobStore(str(tmp_path / "cron.db"))
    _row(store, "old0", datetime(2026, 9, 1, 9), {})
    _row(store, "old1", datetime(2026, 9, 23, 17, 0), {"deliver": "telegram"})
    _row(store, "den0", datetime(2026, 10, 6, 12), {"group": {"surface": "telegram"}})
    _row(store, "agt0", datetime(2026, 9, 1), {"authored_by": "agent"})
    _row(store, "own0", datetime(2026, 9, 1), {"authored_by": "owner"})
    return str(tmp_path / "cron.db")


def _payloads(path):
    con = sqlite3.connect(path)
    try:
        return {r[0]: json.loads(r[1]) for r in con.execute("SELECT id, payload FROM cron_jobs")}
    finally:
        con.close()


def test_dry_run_prints_the_plan_and_writes_nothing(db, capsys):
    from cron import stamp_authorship as sa
    before = _payloads(db)
    assert sa.main(["--db", db, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "den0" in out and "-> owner" in out and "old0" in out and "legacy_unstamped" in out
    assert "agt0" not in out and "own0" not in out
    assert "3 unstamped row(s): 1 -> owner, 2 -> agent (legacy)" in out
    assert _payloads(db) == before


def test_apply_stamps_by_date_and_is_idempotent(db):
    from cron import stamp_authorship as sa
    assert sa.main(["--db", db]) == 0
    p = _payloads(db)
    assert p["den0"]["authored_by"] == "owner" and "legacy_unstamped" not in p["den0"]
    assert p["den0"]["group"] == {"surface": "telegram"}
    for jid in ("old0", "old1"):
        assert p[jid]["authored_by"] == "agent" and p[jid]["legacy_unstamped"] is True
    assert p["agt0"] == {"authored_by": "agent"} and p["own0"] == {"authored_by": "owner"}
    # second run: nothing left to stamp, nothing changes
    assert sa.plan(db) == []
    assert sa.main(["--db", db]) == 0
    assert _payloads(db) == p


def test_a_legacy_row_is_listed_by_adopt_and_the_den_job_is_not(db, tmp_path):
    from cron import stamp_authorship as sa
    sa.main(["--db", db])
    from surfaces.telegram.adopt_ops import adopt_reply
    listing = adopt_reply("rob", str(tmp_path), [])
    assert "old0" in listing and "old1" in listing and "agt0" in listing
    assert "den0" not in listing and "own0" not in listing
