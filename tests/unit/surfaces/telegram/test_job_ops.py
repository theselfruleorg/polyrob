"""`/jobs` and `/job …` (080 WS-2) — one handler for Telegram, the console chat
box and the REPL. Asserts the plumbing and the honesty rules: one grammar,
owner words, a paused job shows no next run, a stop records the seat, and the
scheduler-off sentence is the ONE text."""
import pytest

from surfaces.telegram import job_ops

_ADDR = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setattr("tools.cronjob_tools.cron_enabled", lambda: True)
    return str(tmp_path)


def _jobs(env, user="u1"):
    from core.runtime_paths import cron_db_path
    from cron.jobs import CronJobStore
    return CronJobStore(cron_db_path(env)).list(user_id=user)


def _r(env, *args, user="u1"):
    return job_ops.jobs_reply(user, env, list(args))


def test_empty_list_teaches_the_one_grammar(env):
    out = _r(env)
    assert "No scheduled jobs" in out and "/job new <when> <what>" in out


@pytest.mark.parametrize("line", [
    "every monday 09:00 | summarise the week",
    "'every monday 09:00' summarise the week",
    "every monday 09:00 summarise the week",
])
def test_new_accepts_quotes_and_bar(env, line):
    out = _r(env, "new", *line.split())
    assert "Scheduled job" in out, out
    (job,) = _jobs(env)
    assert job.schedule_spec == "every monday 09:00" and job.task == "summarise the week"


def test_list_shows_schedule_next_and_last(env):
    _r(env, "new", "30m", "|", "check", "the", "inbox")
    out = _r(env)
    assert "1 scheduled job(s)" in out
    assert "check the inbox" in out and "30m" in out
    assert "next " in out and "last never run" in out
    assert "cron" not in out.lower()


def test_a_duplicate_new_warns_and_adds_nothing(env):
    _r(env, "new", "30m", "|", "check", "the", "inbox")
    out = _r(env, "new", "30m", "check", "the", "inbox")
    assert "already does this" in out
    assert len(_jobs(env)) == 1


def test_pause_resume_run_stop_round_trip(env):
    _r(env, "new", "1h", "|", "digest")
    (job,) = _jobs(env)
    short = job.id[:8]
    assert "Paused job" in _r(env, "pause", short)
    listing = _r(env)
    assert "next paused" in listing
    assert "paused" in _r(env, "run", short) and "resume" in _r(env, "run", short)
    assert "Resumed job" in _r(env, "resume", short)
    assert "next scheduler pass" in _r(env, "run", short)
    assert "Stopped job" in _r(env, "stop", short)
    (row,) = _jobs(env)
    assert row.status == "cancelled" and row.payload["stopped"]["via"] == "telegram"
    # A stopped job is not live: it leaves the list and cannot be paused.
    assert "No scheduled jobs" in _r(env)
    assert "no match" in _r(env, "pause", short)


def test_the_seat_is_recorded(env):
    job_ops.jobs_reply("u1", env, ["new", "1h", "|", "digest"], via="repl")
    (job,) = _jobs(env)
    job_ops.jobs_reply("u1", env, ["pause", job.id[:8]], via="repl")
    assert _jobs(env)[0].payload["paused"]["via"] == "repl"


def test_scheduler_off_is_the_one_sentence(env, monkeypatch):
    monkeypatch.setattr("tools.cronjob_tools.cron_enabled", lambda: False)
    out = _r(env, "new", "30m", "|", "check", "the", "treasury")
    assert "Scheduled job" in out
    assert "scheduler is off" in out and "AUTONOMY_POSTURE full" in out
    assert "CRON_ENABLED" not in out
    assert "scheduler is off" in _r(env)  # the list says it too


def test_bad_schedule_and_missing_what(env):
    assert _r(env, "new", "not-a-schedule", "|", "do", "it").startswith("❌")
    assert _r(env, "new", "30m").startswith("❌")
    assert _jobs(env) == []


def test_tenant_scoped(env):
    _r(env, "new", "30m", "|", "mine")
    assert "No scheduled jobs" in _r(env, user="other")


def test_new_carries_tools_and_target(env):
    out = _r(env, "new", "1d", "|", "buy", "back", f"target={_ADDR}", "chain=base",
             "tools=defi_trade,defi_data")
    assert "Scheduled job" in out and "Granted:" in out
    (job,) = _jobs(env)
    assert job.task == "buy back"
    assert job.payload["tools"] == ["defi_trade", "defi_data"]
    assert job.payload["target_token"]["address"].lower() == _ADDR


@pytest.mark.parametrize("opts, needle", [
    ([f"target={_ADDR}"], "chain="),
    (["target=0xnothex", "chain=base"], ""),
    (["chain=base"], "target="),
])
def test_new_refuses_a_bad_target(env, opts, needle):
    out = _r(env, "new", "1d", "|", "buy", *opts)
    assert out.startswith("❌") and needle in out
    assert _jobs(env) == []


def test_edit_changes_one_field_and_keeps_the_payload(env):
    _r(env, "new", "1d", "|", "buy", "back", "tools=defi_trade")
    (job,) = _jobs(env)
    assert "Edited" in _r(env, "edit", job.id[:8], "schedule", "2h")
    assert "what it does" in _r(env, "edit", job.id[:8], "what", "sell", "half")
    (job,) = _jobs(env)
    assert job.schedule_spec == "2h" and job.task == "sell half"
    assert job.payload["tools"] == ["defi_trade"]
    assert "Usage" in _r(env, "edit", job.id[:8], "rig", "x")


def test_show_names_who_paused(env):
    _r(env, "new", "1h", "|", "digest")
    (job,) = _jobs(env)
    _r(env, "pause", job.id[:8])
    assert "paused via telegram" in _r(env, "show", job.id[:8])
