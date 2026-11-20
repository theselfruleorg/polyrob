"""122 W1: the owner's report of the agent's own acts (``report.acts``)."""
import asyncio

import pytest

from core import act_report
from core.effects import record_external_write

HOUR = 3600
T0 = 1_800_000_000 - (1_800_000_000 % 86400)       # a UTC midnight


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "telemetry_events.db"))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    import core.event_log as el
    el._INSTANCES.clear()
    log = el.get_event_log()
    clock = {"t": T0}
    monkeypatch.setattr("core.event_log.time.time", lambda: clock["t"])
    return log, clock


def _act(clock, at, *, effect="social", action="twitter_post", autonomous=True,
         why="answer the poll", user="owner"):
    clock["t"] = at
    record_external_write(effect=effect, tool="twitter", action=action, autonomous=autonomous,
                          user_id=user, session_id="s",
                          extra={"why": why, "params": '{"tweet_id": "1"}',
                                 "source": "job j1"})


def _tick(mode, now, monkeypatch, sent):
    monkeypatch.setattr(act_report, "report_mode", lambda uid, home=None: mode)

    async def deliver(container, uid, text, **kw):
        sent.append((uid, text, kw.get("source")))
        return "sent"
    return asyncio.run(act_report.tick(None, "owner", now=now, deliver=deliver))


def test_money_only_sends_no_report(log, monkeypatch):
    _, clock = log
    _act(clock, T0 + 10)
    sent = []
    assert _tick("money_only", T0 + HOUR + 5, monkeypatch, sent) == "off"
    assert sent == []


def test_every_act_sends_the_last_hour_once_with_the_reason(log, monkeypatch):
    _, clock = log
    _act(clock, T0 + 10)
    _act(clock, T0 + 20, effect="money", action="defi_trade_swap")        # reported at once
    _act(clock, T0 + 30, autonomous=False, why="")                        # the owner's own act
    _act(clock, T0 + HOUR + 1)                                            # next period
    sent = []
    clock["t"] = T0 + HOUR + 5
    assert _tick("every_act", T0 + HOUR + 5, monkeypatch, sent) == "sent"
    assert len(sent) == 1
    uid, text, source = sent[0]
    assert uid == "owner" and source == "act_report"
    assert "(1 act;" in text and "twitter_post" in text and "defi_trade_swap" not in text
    assert "why: `answer the poll`" in text and "job j1" in text
    # The same period is never sent twice, a restart included.
    assert _tick("every_act", T0 + HOUR + 200, monkeypatch, sent) == "not_due"
    assert len(sent) == 1


def test_a_quiet_period_sends_nothing_but_moves_on(log, monkeypatch):
    _, clock = log
    sent = []
    clock["t"] = T0 + HOUR + 5
    assert _tick("every_act", T0 + HOUR + 5, monkeypatch, sent) == "empty"
    assert _tick("every_act", T0 + HOUR + 6, monkeypatch, sent) == "not_due"
    assert sent == []


def test_digest_reports_a_day(log, monkeypatch):
    _, clock = log
    _act(clock, T0 + 5 * HOUR)
    _act(clock, T0 + 20 * HOUR, action="message")
    sent = []
    clock["t"] = T0 + 86400 + 60
    assert _tick("digest", T0 + 86400 + 60, monkeypatch, sent) == "sent"
    assert "last day (2 acts" in sent[0][1]


def test_an_unreadable_log_is_said_never_read_as_no_acts(log, monkeypatch):
    _, clock = log
    monkeypatch.setattr(act_report, "other_acts", lambda *a, **k: None)
    sent = []
    clock["t"] = T0 + HOUR + 5
    assert _tick("every_act", T0 + HOUR + 5, monkeypatch, sent) == "unreadable"
    assert "unreadable" in sent[0][1] and "not \"no acts\"" in sent[0][1]


def test_the_report_mode_reads_the_owner_preference(monkeypatch):
    from core import prefs
    monkeypatch.setattr(prefs, "resolve", lambda key, *a, **k: "every_act"
                        if key == "report.acts" else None)
    assert act_report.report_mode("owner") == "every_act"
    monkeypatch.setattr(prefs, "resolve", lambda key, *a, **k: "nonsense")
    assert act_report.report_mode("owner") == act_report.DEFAULT_MODE
