"""Work › Log (2026-11-20 walk): owner words for machine kinds, the owner's own
acts as their own class, and the act's reason (122 W1) on the rows that carry one.

The live walk showed ``console_goal_verb``, ``console_cron_cancel``,
``cron_cancelled``, ``console_pref_write``, ``owner_command`` and
``goal status_paused: …`` as "What happened", and the owner's own console acts
under "The agent itself".
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.event_kinds as ek
from webview.copy import t


@pytest.fixture(autouse=True)
def _no_ambient_owner(monkeypatch):
    for name in ("POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID",
                 "POLYROB_LOCAL_OWNER", "SURFACE_SUPER_ADMIN_USER_IDS"):
        monkeypatch.delenv(name, raising=False)


def _client(monkeypatch, events, job_names=None):
    import webview.webgate as webgate
    import webview.worklog_api as mod
    monkeypatch.setattr(webgate, "posture", lambda: "local")
    monkeypatch.setattr(webgate, "read_only", lambda: True)
    monkeypatch.setattr(mod, "_recent_events", lambda limit: list(events))
    monkeypatch.setattr(mod, "_job_names", lambda user_id: dict(job_names or {}))
    app = FastAPI()
    app.include_router(mod.router)
    return TestClient(app)


def _event(kind, payload, ts=1.0, summary=None):
    return {"id": f"{kind}:{ts}", "ts": ts, "source": "telemetry", "user_id": "",
            "session_id": "", "kind": kind, "summary": summary or kind,
            "payload": payload}


# --- the words -------------------------------------------------------------- #

def test_owner_words_name_the_act_not_the_kind():
    from webview.worklog_api import owner_words
    names = {"j1": "Morning digest"}
    assert owner_words(ek.CONSOLE_CRON_CANCEL, {"job_id": "j1"}, names) == (
        "You stopped the scheduled job “Morning digest”.")
    assert owner_words(ek.CRON_CANCELLED, {"job_id": "j1", "via": "agent"}, names) == (
        "The agent stopped the scheduled job “Morning digest”.")
    assert owner_words(ek.CRON_CANCELLED, {"job_id": "j1", "via": "webview"}, names) == (
        "You stopped the scheduled job “Morning digest”.")
    assert owner_words(ek.CONSOLE_GOAL_VERB,
                       {"verb": "pause", "title": "Write the report"}) == (
        "You pressed “Pause” on the goal “Write the report”.")
    assert owner_words(ek.OWNER_COMMAND, {"verb": "/status", "seat": "telegram"}) == (
        "You sent the command /status from Telegram.")
    assert owner_words("goal_status_paused", {"title": "Write the report"}) == (
        "The goal “Write the report” is paused.")


def test_a_job_is_named_never_by_its_id():
    from webview.worklog_api import owner_words
    # The name rides on the console's own record, else the store's name; never the id.
    said = owner_words(ek.CONSOLE_CRON_CANCEL, {"job_id": "2d02db6b8071", "name": "Digest"})
    assert "Digest" in said and "2d02db6b8071" not in said
    unknown = owner_words(ek.CRON_PAUSED, {"job_id": "2d02db6b8071"}, {})
    assert "2d02db6b8071" not in unknown and t("work.log.a_job") in unknown


def test_a_pref_write_names_the_setting_in_its_owner_label():
    from webview.worklog_api import owner_words
    said = owner_words(ek.CONSOLE_PREF_WRITE, {"key": "ui.theme"})
    assert t("pref.ui.theme.label") in said


@pytest.mark.parametrize("kind", sorted(k for k in ek.KNOWN_KINDS
                                        if k.startswith("console_")) + [ek.OWNER_COMMAND])
def test_every_owner_kind_has_words(kind):
    """A new console write must come with its words, or it reads as a machine name."""
    from webview.worklog_api import owner_words
    said = owner_words(kind, {})
    assert said and kind not in said


def test_a_kind_with_no_words_keeps_the_stream_summary():
    from webview.worklog_api import owner_words
    assert owner_words(ek.WALLET_SPEND, {"amount_usd": 1}) == ""


# --- the endpoint ------------------------------------------------------------ #

def test_the_log_shows_owner_words_and_the_owner_class(monkeypatch):
    events = [
        _event(ek.CONSOLE_CRON_CANCEL, {"job_id": "j1"}, ts=3.0),
        _event(ek.OWNER_COMMAND, {"verb": "/jobs", "seat": "console"}, ts=2.0),
        _event("goal_status_paused", {"goal_id": "g1", "title": "Plan"}, ts=1.0,
               summary="goal status_paused: Plan (g1)"),
    ]
    body = _client(monkeypatch, events, {"j1": "Digest"}).get("/api/webgate/log").json()
    rows = {e["kind"]: e for e in body["entries"]}
    assert rows[ek.CONSOLE_CRON_CANCEL]["summary"] == "You stopped the scheduled job “Digest”."
    assert rows[ek.CONSOLE_CRON_CANCEL]["cls"] == "owner"
    assert rows[ek.OWNER_COMMAND]["cls"] == "owner"
    assert rows["goal_status_paused"]["summary"] == "The goal “Plan” is paused."
    assert "owner" in body["classes"]


def test_the_act_reason_rides_on_the_rows_that_carry_one(monkeypatch):
    events = [
        _event(ek.EXTERNAL_WRITE, {"action": "twitter_post", "effect": "social",
                                   "why": "the weekly update is due"}, ts=2.0),
        _event(ek.EXTERNAL_WRITE, {"action": "send_email", "effect": "comms"}, ts=1.0),
    ]
    body = _client(monkeypatch, events).get("/api/webgate/log").json()
    with_why, without = body["entries"]
    assert with_why["why"] == "the weekly update is due"
    assert "why" not in without          # no invented reason


def test_the_reason_is_read_only_from_the_act_record():
    from webview.worklog_api import act_why
    assert act_why(ek.EXTERNAL_WRITE, {"why": "  two\nlines "}) == "two lines"
    assert act_why(ek.CRON_RUN, {"why": "not an act record"}) == ""
    assert act_why(ek.EXTERNAL_WRITE, None) == ""


def test_the_owner_chip_has_words():
    assert t("work.log.class_owner")
