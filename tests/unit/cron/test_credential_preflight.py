"""Credential preflight (X evaluation 2026-09-26, P2-1 / build item 9).

After the X OAuth 2.0 login died on 09-25, six X crons kept paying for full LLM
turns that ended "0 touches" (~$3.9 in 8 days) while verdicts.db already held
the fact. A cron whose rail needs a credential with an OPEN verdict is now a
$0 tick, and the owner is told ONCE per verdict episode.
"""
import pytest

from core import credential_verdicts as cv
from cron.credential_preflight import NEEDS, REASON
from cron.jobs import CronJob
from cron.preflight import preflight_skip


def _job(task="DM CHECK: read new X DMs with twitter_get_dms and answer", payload=None,
         jid="dm1"):
    return CronJob(id=jid, user_id="u1", task=task, schedule_spec="12h",
                   next_run_at=None, payload=payload or {})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("AUTONOMOUS_RIG_DEFAULT", raising=False)
    monkeypatch.delenv("X_BROWSER_ENABLED", raising=False)


def _dead_login():
    cv.record_rejection("x_oauth2", "", code="relogin_needed",
                        remedy="/x login or `polyrob x-account oauth-login`")


def test_the_table_is_data_not_ifs():
    kinds = {row.kind for row in NEEDS}
    assert {"x_oauth2", "twitter_api"} <= kinds


def test_a_dm_cron_skips_at_zero_while_the_x_login_is_dead(tmp_path):
    _dead_login()
    out = preflight_skip(_job(), data_dir=str(tmp_path))
    assert out is not None and out.reason == REASON
    assert out.attrs == {"preflight": "credential", "verdict": "x_oauth2",
                         "verdict_code": "relogin_needed"}
    assert "/x login" in out.notice and "dm1" in out.notice


def test_the_owner_is_told_once_per_verdict_not_per_run(tmp_path):
    _dead_login()
    first = preflight_skip(_job(), data_dir=str(tmp_path))
    second = preflight_skip(_job(), data_dir=str(tmp_path))
    other = preflight_skip(_job(jid="dm2"), data_dir=str(tmp_path))
    assert first.notice
    assert second is not None and second.notice is None
    assert other is not None and other.notice is None


def test_a_new_episode_notifies_again(tmp_path):
    _dead_login()
    assert preflight_skip(_job(), data_dir=str(tmp_path)).notice
    cv.clear_rejection("x_oauth2", "")
    cv._reset_for_tests()
    import time
    time.sleep(0.01)
    _dead_login()
    assert preflight_skip(_job(), data_dir=str(tmp_path)).notice


def test_no_skip_when_the_verdict_is_closed(tmp_path):
    _dead_login()
    cv.clear_rejection("x_oauth2", "")
    assert preflight_skip(_job(), data_dir=str(tmp_path)) is None


def test_no_skip_for_a_non_dm_x_cron(tmp_path):
    """The OAuth 1.0a keys still post: an ENGAGEMENT cron is not a DM cron."""
    _dead_login()
    job = _job(task="ENGAGEMENT: reply to 3 mentions", payload={"tools": ["twitter"]})
    assert preflight_skip(job, data_dir=str(tmp_path)) is None


def test_no_skip_when_the_declared_tools_do_not_include_twitter(tmp_path):
    _dead_login()
    job = _job(payload={"tools": ["email", "message"]})
    assert preflight_skip(job, data_dir=str(tmp_path)) is None


def test_a_store_written_after_the_refusal_runs_the_tick(tmp_path):
    """The pack treats a newer token record as a re-login it could not clear."""
    import os
    import time
    _dead_login()
    p = tmp_path / ".x_session.json"
    p.write_text("{}")
    later = time.time() + 5
    os.utime(p, (later, later))
    assert preflight_skip(_job(), data_dir=str(tmp_path)) is None


def test_x_api_402_skips_an_x_cron_while_the_hold_is_live(tmp_path):
    cv.record_rejection("twitter_api", "search", code="402", remedy="top up X API credits")
    job = _job(task="TARGET COLLECTION: search twitter for builders", jid="tc1")
    out = preflight_skip(job, data_dir=str(tmp_path))
    assert out is not None and out.attrs["verdict"] == "twitter_api"


def test_x_api_402_does_not_skip_an_unrelated_cron(tmp_path):
    cv.record_rejection("twitter_api", "search", code="402")
    job = _job(task="Summarise the treasury ledger", jid="t1")
    assert preflight_skip(job, data_dir=str(tmp_path)) is None


def test_x_api_402_does_not_skip_when_the_browser_rail_serves(tmp_path, monkeypatch):
    monkeypatch.setenv("X_BROWSER_ENABLED", "true")
    cv.record_rejection("twitter_api", "search", code="402")
    job = _job(task="post on X about the release", jid="p1")
    assert preflight_skip(job, data_dir=str(tmp_path)) is None


def test_a_lapsed_402_hold_runs_one_probe_tick(tmp_path, monkeypatch):
    """Nothing else re-probes a topped-up account: once the hold lapses the
    tick runs, and its success clears the verdict."""
    cv.record_rejection("twitter_api", "search", code="402")
    real = cv.active

    def lapsed(kind=None, *, live_only=False):
        return [] if live_only else real(kind)
    monkeypatch.setattr(cv, "active", lapsed)
    job = _job(task="search twitter for builders", jid="tc1")
    assert preflight_skip(job, data_dir=str(tmp_path)) is None


def test_delivery_jobs_are_never_credential_preflighted(tmp_path):
    _dead_login()
    assert preflight_skip(_job(payload={"deliver": "telegram"}), data_dir=str(tmp_path)) is None


def test_an_error_fails_open(tmp_path, monkeypatch):
    _dead_login()

    def boom(*a, **k):
        raise RuntimeError("verdict store down")
    monkeypatch.setattr(cv, "active", boom)
    assert preflight_skip(_job(), data_dir=str(tmp_path)) is None


@pytest.mark.asyncio
async def test_runner_skips_at_zero_and_notifies_once(tmp_path, monkeypatch):
    from core import event_log as el
    import core.surfaces.user_delivery as ud
    from cron.runner import make_agent_runner
    monkeypatch.setenv("CRON_RUN_LOOP", "true")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(el, "_INSTANCES", {})
    log = el.TelemetryEventLog(str(tmp_path / "te.db"))
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: log)
    monkeypatch.setattr(el, "event_log_enabled", lambda: True)
    sent = []

    async def fake_deliver(container, user_id, text, **kw):
        sent.append((user_id, text, kw.get("source")))
        return "sent"
    monkeypatch.setattr(ud, "deliver_user_message", fake_deliver)
    _dead_login()
    calls = []

    class _TaskAgent:
        async def create_session(self, *a, **k):
            calls.append("create"); return {"id": "s1"}

        async def run_session(self, *a, **k):
            calls.append("run"); return "x"

    runner = make_agent_runner(_TaskAgent(), data_dir=str(tmp_path))
    assert await runner(_job()) is True
    assert await runner(_job()) is True
    assert calls == []
    rows = log.query(kind="cron_run")
    assert [(r["attrs"]["outcome"], r["attrs"]["reason"]) for r in rows] == \
        [("skipped", REASON)] * 2
    assert len(sent) == 1
    assert sent[0][0] == "u1" and sent[0][2] == "cron_preflight"


_DEN_TASK = ("DEN ENGAGEMENT — periodic replies in The Public Den (telegram:-1002125904710). "
             "Read the den with room_read(room='-1002125904710', limit=40). DO NOT REPLY IF: "
             "the newest lines are only joins, spam, shill pitches, or forwarded promo "
             "(e.g. \"100x gems\", \"promote your project\", \"can I get a dm\"). "
             "Post with message(surface='telegram', target='-1002125904710', text=...).")


#: the prod `social` rig (the X pack adds `twitter`); explicit so the test does
#: not depend on pack discovery in the running tree.
_SOCIAL = {"tools": ["twitter", "room_read", "message"]}


def test_a_quoted_dm_in_a_telegram_jobs_spam_list_is_not_an_x_dm_need(tmp_path):
    # live 2026-09-29: DEN ENGAGEMENT (rig social) was skipped 3x as needing the
    # X DM login because its own "do not engage" example contained "a dm".
    _dead_login()
    job = _job(task=_DEN_TASK, payload=_SOCIAL, jid="den1")
    assert preflight_skip(job, data_dir=str(tmp_path)) is None


def test_the_real_dm_check_job_still_needs_the_x_login(tmp_path):
    _dead_login()
    task = ("DM CHECK — X/Twitter inbox. STEPS: 1) Read the inbox ONCE via "
            "twitter_get_dms. TEMPLATE SPAM (\"DM me\") -> skip.")
    out = preflight_skip(_job(task=task, payload=_SOCIAL, jid="dmc"),
                         data_dir=str(tmp_path))
    assert out is not None and out.attrs["verdict"] == "x_oauth2"


@pytest.mark.parametrize("task", [
    "check X DMs and answer genuine ones",
    "read the twitter DMs once",
    "reply to direct messages on X",
    "open the X chat inbox",
])
def test_plain_x_dm_phrasings_still_match(tmp_path, task):
    _dead_login()
    assert preflight_skip(_job(task=task, payload=_SOCIAL, jid="p"),
                          data_dir=str(tmp_path)) is not None


def test_a_prohibited_dm_is_not_a_dm_need(tmp_path):
    # X TARGET COLLECTION: "RESEARCH ONLY: no outreach, no DMs, no follows".
    _dead_login()
    job = _job(task="X TARGET COLLECTION. RESEARCH ONLY: no outreach, no DMs, no follows.",
               payload=_SOCIAL, jid="tc")
    assert preflight_skip(job, data_dir=str(tmp_path)) is None


def test_an_outreach_job_that_sends_opt_in_dms_still_needs_the_login(tmp_path):
    _dead_login()
    task = ('X OUTREACH. 1. Phase 2 — opt-in DMs. ONLY accounts whose bio carries '
            '"DM for collabs / DMs open".')
    assert preflight_skip(_job(task=task, payload=_SOCIAL, jid="xo"),
                          data_dir=str(tmp_path)) is not None


def test_the_den_moderation_task_text_is_not_an_x_dm_need(tmp_path):
    # A room-moderation task quotes "can I get a dm" as a shill example; a
    # quote broken across a line would have skipped every run of the job.
    _dead_login()
    task = ("ROOM ENGAGEMENT — periodic replies AND moderation in the room.\n"
            "2. MODERATE FIRST. Irrelevant shill is not allowed:\n"
            "   - unsolicited promo or marketing offers (\"promote your project\", \"KOL\",\n"
            "     \"I can get you listed\", \"can I get a dm\", paid-post pitches);\n"
            "   - repeated flood of the same message.\n")
    assert preflight_skip(_job(task=task, payload=_SOCIAL, jid="den2"),
                          data_dir=str(tmp_path)) is None
