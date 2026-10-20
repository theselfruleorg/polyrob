"""Credential preflight (X evaluation 2026-09-26, P2-1; task-agnostic 2026-10-14).

A cron whose DECLARED tool has a live credential verdict is a $0 tick, and the
owner is told ONCE per verdict episode. The task text is never read: a
"full"/undeclared toolset always runs.
"""
import pytest

from core import credential_verdicts as cv
from cron.credential_preflight import REASON
from cron.jobs import CronJob
from cron.preflight import preflight_skip
from tools import tool_disclosure as td


def _job(task="any task text", payload=None, jid="j1"):
    return CronJob(id=jid, user_id="u1", task=task, schedule_spec="12h",
                   next_run_at=None, payload=payload if payload is not None
                   else {"tools": ["twitter", "message"]})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("AUTONOMOUS_RIG_DEFAULT", raising=False)
    monkeypatch.setitem(td.CREDENTIAL_GATED_TOOLS, "twitter",
                        ("twitter_api", "top up X API credits"))


def _dead_api():
    cv.record_rejection("twitter_api", "search", code="402", remedy="top up X API credits")


def test_a_declared_tool_with_a_live_verdict_skips_at_zero(tmp_path):
    _dead_api()
    out = preflight_skip(_job(), data_dir=str(tmp_path))
    assert out is not None and out.reason == REASON
    assert out.attrs == {"preflight": "credential", "verdict": "twitter_api",
                         "verdict_code": "402"}
    assert "top up" in out.notice and "j1" in out.notice


def test_the_owner_is_told_once_per_verdict_not_per_run(tmp_path):
    _dead_api()
    first = preflight_skip(_job(), data_dir=str(tmp_path))
    second = preflight_skip(_job(), data_dir=str(tmp_path))
    other = preflight_skip(_job(jid="j2"), data_dir=str(tmp_path))
    assert first.notice
    assert second is not None and second.notice is None
    assert other is not None and other.notice is None


def test_a_new_episode_notifies_again(tmp_path):
    _dead_api()
    assert preflight_skip(_job(), data_dir=str(tmp_path)).notice
    cv.clear_rejection("twitter_api", "search")
    cv._reset_for_tests()
    import time
    time.sleep(0.01)
    _dead_api()
    assert preflight_skip(_job(), data_dir=str(tmp_path)).notice


def test_no_skip_when_the_verdict_is_closed(tmp_path):
    _dead_api()
    cv.clear_rejection("twitter_api", "search")
    assert preflight_skip(_job(), data_dir=str(tmp_path)) is None


def test_no_skip_when_the_declared_tools_have_no_verdict(tmp_path):
    _dead_api()
    job = _job(payload={"tools": ["email", "message"]})
    assert preflight_skip(job, data_dir=str(tmp_path)) is None


@pytest.mark.parametrize("task", [
    "DM CHECK: read new X DMs with twitter_get_dms",
    "post on X about the release",
    "(e.g. \"can I get a dm\")",
])
def test_the_task_text_is_never_read_for_a_full_toolset(tmp_path, task):
    _dead_api()
    assert preflight_skip(_job(task=task, payload={}), data_dir=str(tmp_path)) is None
    assert preflight_skip(_job(task=task, payload={"rig": "full"}),
                          data_dir=str(tmp_path)) is None


def test_a_lapsed_hold_runs_one_probe_tick(tmp_path, monkeypatch):
    """Nothing else re-probes a topped-up account: once the hold lapses the
    tick runs, and its success clears the verdict."""
    _dead_api()
    real = cv.active

    def lapsed(kind=None, *, live_only=False):
        return [] if live_only else real(kind)
    monkeypatch.setattr(cv, "active", lapsed)
    assert preflight_skip(_job(), data_dir=str(tmp_path)) is None


def test_delivery_jobs_are_never_credential_preflighted(tmp_path):
    _dead_api()
    assert preflight_skip(_job(payload={"deliver": "telegram", "tools": ["twitter"]}),
                          data_dir=str(tmp_path)) is None


def test_an_error_fails_open(tmp_path, monkeypatch):
    _dead_api()

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
    _dead_api()
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
