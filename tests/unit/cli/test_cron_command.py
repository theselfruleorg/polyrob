"""`polyrob cron` — owner cron management (parity G4).

The 2026-07-12 UI-surface review: cron jobs could not be created/cancelled from
ANY human surface — REPL /cron, webview /autonomy and Telegram /status are all
read-only, so scheduling was agent-tool-only. This group rides the SAME
``cron.service.CronService`` + ``cron.db`` (at ``core.runtime_paths.
resolve_data_home()``) the lifespan ticker and the webview endpoint read, so a
job scheduled here is exactly what the ticker will run.
"""
from click.testing import CliRunner


def _invoke(args, tmp_path, extra_env=None):
    from cli.commands.cron import cron
    env = {"POLYROB_DATA_DIR": str(tmp_path)}
    env.update(extra_env or {})
    return CliRunner().invoke(cron, args, env=env)


def _jobs(tmp_path, user_id=None):
    from cron.jobs import CronJobStore
    return CronJobStore(str(tmp_path / "cron.db")).list(user_id=user_id)


def test_schedule_persists_a_job_the_store_can_read(tmp_path):
    res = _invoke(["schedule", "check the feeds", "30m", "--user", "u1"], tmp_path)
    assert res.exit_code == 0, res.output
    jobs = _jobs(tmp_path, "u1")
    assert len(jobs) == 1
    assert jobs[0].task == "check the feeds"
    assert jobs[0].schedule_spec == "30m"
    assert jobs[0].next_run_at is not None


def test_schedule_bad_spec_is_a_clear_error(tmp_path):
    res = _invoke(["schedule", "x", "not-a-schedule", "--user", "u1"], tmp_path)
    assert res.exit_code != 0
    assert "schedule" in res.output.lower()
    assert _jobs(tmp_path) == []


def test_schedule_warns_when_cron_disabled(tmp_path):
    res = _invoke(["schedule", "x", "30m", "--user", "u1"], tmp_path,
                  extra_env={"CRON_ENABLED": ""})
    assert res.exit_code == 0, res.output
    assert "CRON_ENABLED" in res.output


def test_list_shows_the_job(tmp_path):
    _invoke(["schedule", "check the feeds", "30m", "--user", "u1"], tmp_path)
    res = _invoke(["list", "--user", "u1"], tmp_path)
    assert res.exit_code == 0, res.output
    assert "check the feeds" in res.output
    assert "30m" in res.output


def test_show_prints_full_job(tmp_path):
    _invoke(["schedule", "check the feeds", "30m", "--user", "u1"], tmp_path)
    job_id = _jobs(tmp_path, "u1")[0].id
    res = _invoke(["show", job_id, "--user", "u1"], tmp_path)
    assert res.exit_code == 0, res.output
    assert job_id in res.output
    assert "check the feeds" in res.output


def test_cancel_marks_job_cancelled_tenant_scoped(tmp_path):
    _invoke(["schedule", "check the feeds", "30m", "--user", "u1"], tmp_path)
    job_id = _jobs(tmp_path, "u1")[0].id
    # wrong tenant: refused
    res = _invoke(["cancel", job_id, "--user", "u2"], tmp_path)
    assert "no " in res.output.lower() or res.exit_code != 0
    assert _jobs(tmp_path, "u1")[0].status == "scheduled"
    # right tenant: cancelled
    res = _invoke(["cancel", job_id, "--user", "u1"], tmp_path)
    assert res.exit_code == 0, res.output
    assert _jobs(tmp_path, "u1")[0].status == "cancelled"


def test_cron_registered_in_group():
    from cli.polyrob import cli
    assert "cron" in cli.commands
