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
    assert "cron" in cli.list_commands(None)


def test_edit_max_duration_tenant_scoped_and_capped(tmp_path):
    # 2026-09-17: the EXIT/SCOUT treasury rails were created with a 240 s cap
    # and timed out on 22/24 runs; the only way to raise it was a raw sqlite
    # UPDATE on prod. This verb is that edit, tenant-scoped and capped like the
    # agent tool (≤1800 s since 2026-09-18).
    _invoke(["schedule", "check the feeds", "30m", "--user", "u1",
             "--max-duration", "240"], tmp_path)
    job_id = _jobs(tmp_path, "u1")[0].id
    # wrong tenant: refused, unchanged
    res = _invoke(["edit", job_id, "--max-duration", "600", "--user", "u2"], tmp_path)
    assert res.exit_code != 0
    assert _jobs(tmp_path, "u1")[0].max_duration_seconds == 240
    # over the cap: refused, unchanged
    res = _invoke(["edit", job_id, "--max-duration", "1801", "--user", "u1"], tmp_path)
    assert res.exit_code != 0
    assert _jobs(tmp_path, "u1")[0].max_duration_seconds == 240
    # right tenant, in range: applied and echoed
    res = _invoke(["edit", job_id, "--max-duration", "600", "--user", "u1"], tmp_path)
    assert res.exit_code == 0, res.output
    assert "600" in res.output
    assert _jobs(tmp_path, "u1")[0].max_duration_seconds == 600


# --- job-id PREFIX resolution (maint friction 2026-09-21 00:50Z) -------------
# `cron list` prints 8-char ids; `cron edit <8 chars> --rig social` failed with
# "unknown rig or no such job", which blamed the rig. A unique prefix now
# resolves like a goal id does; an ambiguous one names the candidates; the
# rig error and the job error are no longer one sentence.

def test_edit_accepts_a_unique_id_prefix(tmp_path):
    _invoke(["schedule", "watch the tranches", "30m", "--user", "u1"], tmp_path)
    job_id = _jobs(tmp_path, "u1")[0].id
    res = _invoke(["edit", job_id[:8], "--rig", "social", "--user", "u1"], tmp_path)
    assert res.exit_code == 0, res.output
    assert _jobs(tmp_path, "u1")[0].payload.get("rig") == "social"


def test_edit_bad_rig_names_the_rig_not_the_job(tmp_path):
    _invoke(["schedule", "watch the tranches", "30m", "--user", "u1"], tmp_path)
    job_id = _jobs(tmp_path, "u1")[0].id
    res = _invoke(["edit", job_id, "--rig", "banana", "--user", "u1"], tmp_path)
    assert res.exit_code != 0
    assert "unknown rig" in res.output and "banana" in res.output
    assert "no such job" not in res.output


def test_edit_unknown_id_says_no_such_job(tmp_path):
    _invoke(["schedule", "watch the tranches", "30m", "--user", "u1"], tmp_path)
    res = _invoke(["edit", "zzzzzzzz", "--rig", "social", "--user", "u1"], tmp_path)
    assert res.exit_code != 0
    assert "no job" in res.output and "unknown rig" not in res.output


def test_edit_ambiguous_prefix_names_the_candidates(tmp_path, monkeypatch):
    # Force two ids sharing a prefix by seeding the store directly.
    from cron.jobs import CronJobStore, CronJob
    from datetime import datetime, timezone
    store = CronJobStore(str(tmp_path / "cron.db"))
    a = store.list()  # creates the schema
    src = _invoke(["schedule", "one", "30m", "--user", "u1"], tmp_path)
    assert src.exit_code == 0, src.output
    first = _jobs(tmp_path, "u1")[0]
    twin = CronJob(**{**first.__dict__, "id": first.id[:8] + "ffffffff"[: len(first.id) - 8], "task": "two"})
    store.add(twin)
    res = _invoke(["edit", first.id[:8], "--rig", "social", "--user", "u1"], tmp_path)
    assert res.exit_code != 0
    assert "ambiguous" in res.output.lower()
    assert first.id in res.output and twin.id in res.output


def test_show_and_cancel_accept_a_prefix(tmp_path):
    _invoke(["schedule", "watch the tranches", "30m", "--user", "u1"], tmp_path)
    job_id = _jobs(tmp_path, "u1")[0].id
    res = _invoke(["show", job_id[:8], "--user", "u1"], tmp_path)
    assert res.exit_code == 0, res.output
    assert "watch the tranches" in res.output
    res = _invoke(["cancel", job_id[:8], "--user", "u1"], tmp_path)
    assert res.exit_code == 0, res.output
    assert _jobs(tmp_path, "u1")[0].status == "cancelled"


# --- `--deliver` (intel MEDIUM 2026-09-21 01:20Z) ----------------------------
# The hourly SCOUT rail carried `payload.deliver=telegram` from before its own
# rule 8 ("close silently — no owner ping") and DM'd the owner ~20 no-op
# reports a day. The only remedy was a raw sqlite UPDATE on the live db.

def test_edit_deliver_sets_and_drops_the_target_and_keeps_the_rest(tmp_path):
    _invoke(["schedule", "scout", "30m", "--user", "u1"], tmp_path)
    job_id = _jobs(tmp_path, "u1")[0].id
    res = _invoke(["edit", job_id, "--rig", "money_rail", "--user", "u1"], tmp_path)
    assert res.exit_code == 0, res.output
    res = _invoke(["edit", job_id, "--deliver", "telegram", "--user", "u1"], tmp_path)
    assert res.exit_code == 0, res.output
    p = _jobs(tmp_path, "u1")[0].payload
    assert p.get("deliver") == "telegram" and p.get("rig") == "money_rail"
    res = _invoke(["edit", job_id, "--deliver", "none", "--user", "u1"], tmp_path)
    assert res.exit_code == 0, res.output
    assert "deliver" in res.output.lower()
    p = _jobs(tmp_path, "u1")[0].payload
    assert "deliver" not in p and p.get("rig") == "money_rail", "a deliver edit never drops the rig"


def test_edit_deliver_refuses_a_target_outside_the_allowlist(tmp_path):
    _invoke(["schedule", "scout", "30m", "--user", "u1"], tmp_path)
    job_id = _jobs(tmp_path, "u1")[0].id
    res = _invoke(["edit", job_id, "--deliver", "pigeon", "--user", "u1"], tmp_path)
    assert res.exit_code != 0
    assert "pigeon" in res.output and "telegram" in res.output
    assert "deliver" not in _jobs(tmp_path, "u1")[0].payload


# --- `--task-file` (intel HIGH 2026-09-21 04:25Z) ----------------------------
# The buyback rail's task prose named a CANCELLED safety cron id and told the
# reader to take "the most recent reading" from a 2,300-line file whose top
# table had stopped growing — two silent skips against a stale reference. The
# only way to fix prose was cancel + re-schedule (losing last_run_at and every
# payload edit). Task text is now an owner edit like the cap or the rig.

def test_edit_task_file_replaces_the_prose_and_keeps_everything_else(tmp_path):
    _invoke(["schedule", "old prose", "30m", "--user", "u1"], tmp_path)
    job_id = _jobs(tmp_path, "u1")[0].id
    assert _invoke(["edit", job_id, "--rig", "money_rail", "--user", "u1"], tmp_path).exit_code == 0
    before = _jobs(tmp_path, "u1")[0]
    f = tmp_path / "task.txt"
    f.write_text("NEW PROSE — read reports/safety-latest.json first\n")
    res = _invoke(["edit", job_id, "--task-file", str(f), "--user", "u1"], tmp_path)
    assert res.exit_code == 0, res.output
    assert "task" in res.output.lower()
    after = _jobs(tmp_path, "u1")[0]
    assert after.task == "NEW PROSE — read reports/safety-latest.json first"
    assert after.payload.get("rig") == "money_rail"
    assert after.schedule_spec == before.schedule_spec
    assert after.next_run_at == before.next_run_at, "a prose edit never re-times the job"


def test_edit_task_file_refuses_an_empty_file(tmp_path):
    _invoke(["schedule", "old prose", "30m", "--user", "u1"], tmp_path)
    job_id = _jobs(tmp_path, "u1")[0].id
    f = tmp_path / "empty.txt"
    f.write_text("   \n")
    res = _invoke(["edit", job_id, "--task-file", str(f), "--user", "u1"], tmp_path)
    assert res.exit_code != 0
    assert _jobs(tmp_path, "u1")[0].task == "old prose"


def test_edit_task_file_is_tenant_scoped(tmp_path):
    _invoke(["schedule", "old prose", "30m", "--user", "u1"], tmp_path)
    job_id = _jobs(tmp_path, "u1")[0].id
    f = tmp_path / "task.txt"
    f.write_text("hijack")
    res = _invoke(["edit", job_id, "--task-file", str(f), "--user", "u2"], tmp_path)
    assert res.exit_code != 0
    assert _jobs(tmp_path, "u1")[0].task == "old prose"
