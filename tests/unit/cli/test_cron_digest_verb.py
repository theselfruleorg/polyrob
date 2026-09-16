"""`polyrob cron digest` — the public way to schedule the owner daily digest.

Round-2 review. C4 added a health item that fires when `OWNER_DIGEST_ENABLED`
is on with no digest job scheduled, and its remedy named
`scripts/seed_owner_digest.py`. That script is NOT in the published package, so
on a pip install the health item advised a command the reader does not have —
and there was no public verb at all for the one job shape the digest needs
(`payload.digest`). A remedy the reader cannot run is not a remedy.
"""
import pytest
from click.testing import CliRunner


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    return tmp_path


def _run(*args):
    from cli.commands.cron import cron
    return CliRunner().invoke(cron, list(args) + ["--user", "rob"])


def _jobs(tmp_path):
    from core.runtime_paths import cron_db_path
    from cron.jobs import CronJobStore
    from cron.service import CronService
    return CronService(CronJobStore(cron_db_path(str(tmp_path)))).list_jobs(user_id="rob")


def test_digest_schedules_a_digest_payload_job(tmp_path):
    res = _run("digest", "every day 08:00")
    assert res.exit_code == 0, res.output
    jobs = _jobs(tmp_path)
    assert len(jobs) == 1
    assert jobs[0].payload.get("digest") is True
    assert jobs[0].payload.get("wake_agent") is False   # a $0 tick, never a model call
    assert jobs[0].payload.get("deliver") == "telegram"


def test_digest_is_idempotent_and_reschedules(tmp_path):
    _run("digest", "every day 08:00")
    res = _run("digest", "every day 21:00")
    assert res.exit_code == 0, res.output
    jobs = [j for j in _jobs(tmp_path) if j.status != "cancelled"]
    assert len(jobs) == 1, "a second call must move the schedule, not add a second digest"
    assert jobs[0].schedule_spec == "every day 21:00"


def test_digest_off_cancels_it(tmp_path):
    _run("digest", "every day 08:00")
    res = _run("digest", "--off")
    assert res.exit_code == 0, res.output
    assert [j for j in _jobs(tmp_path) if j.status != "cancelled"] == []


def test_digest_off_with_nothing_scheduled_says_so(tmp_path):
    res = _run("digest", "--off")
    assert res.exit_code == 0
    assert "no digest job" in res.output.lower()


def test_an_invalid_schedule_is_a_clean_error(tmp_path):
    res = _run("digest", "every blursday")
    assert res.exit_code != 0
    assert "invalid schedule" in res.output.lower()


def test_the_status_remedy_names_this_verb():
    """The health item and the verb must not drift apart."""
    import inspect
    import core.status_snapshot as ss
    src = inspect.getsource(ss._digest_health)
    assert "polyrob cron digest" in src
    assert "scripts/" not in src, "a published health item may not name the private tree"
