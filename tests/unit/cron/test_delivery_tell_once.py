"""Tell once (owner ruling, 2026-09-21 10:44Z): a cron job's ``deliver`` echo is
SKIPPED when the run itself already told the owner.

Four jobs carried ``deliver=telegram`` AND ``message()``d the owner from inside
the run, so every run produced a pair — the real content at hh:mm and, seconds
later, a "… complete." echo of the final text. The harness can see the first
send: it is a ``user_delivery`` row with the run's own ``session_id`` and
``outcome=sent`` (source ``agent_send``); the echo would be the row with no
session id and source ``cron``. The rail skips the echo when such a row exists
and answers ``already_told`` so the ledger shows the decision; an unreadable
log fails OPEN (the echo goes out, as before) — a store that cannot be read
must not silence a delivery."""
import time

import pytest

from cron import delivery


class _Job:
    id = "job-1"
    user_id = "u1"
    task = "weekly synthesis"


class _Log:
    def __init__(self, rows, *, raise_on_query=False):
        self.rows = rows
        self.raise_on_query = raise_on_query

    def query(self, **kw):
        if self.raise_on_query:
            raise RuntimeError("locked")
        return [r for r in self.rows
                if kw.get("kind") in (None, r["kind"])
                and kw.get("user_id") in (None, r["user_id"])]


def _row(session_id, outcome="sent", source="agent_send", user_id="u1", age=30):
    return {"ts": time.time() - age, "kind": "user_delivery", "user_id": user_id,
            "session_id": session_id, "source": source,
            "attrs": {"outcome": outcome, "text": "the brief is live"}}


def test_a_run_that_already_told_the_owner_is_detected():
    log = _Log([_row("s-1")])
    assert delivery.run_already_told_owner(log, "u1", "s-1") is True


def test_only_this_session_and_only_a_real_send_count():
    log = _Log([_row("s-other"),                      # another run's send
                _row("s-1", outcome="quiet_held"),    # held, the owner has NOT read it
                _row("s-1", source="cron"),           # a cron echo is not the run telling
                _row("s-1", user_id="u2")])           # another tenant
    assert delivery.run_already_told_owner(log, "u1", "s-1") is False


def test_an_unreadable_log_reads_as_not_told():
    assert delivery.run_already_told_owner(_Log([], raise_on_query=True), "u1", "s-1") is False
    assert delivery.run_already_told_owner(None, "u1", "s-1") is False
    assert delivery.run_already_told_owner(_Log([_row("s-1")]), "u1", None) is False


@pytest.mark.asyncio
async def test_the_echo_is_skipped_when_the_run_already_told(monkeypatch):
    sent = []

    async def _tg(task_agent, job, final, deliver_target):
        sent.append(final)
        return "sent"

    monkeypatch.setattr(delivery, "_deliver_telegram", _tg)
    monkeypatch.setattr(delivery, "_event_log_for_read", lambda: _Log([_row("s-1")]))
    surfaced = []
    monkeypatch.setattr(delivery, "_mark_surfaced", lambda sid, uid=None: surfaced.append(sid))
    out = await delivery.deliver_result_ex(object(), _Job(), "Weekly synthesis complete.",
                                           target="telegram", session_id="s-1")
    assert out == "already_told"
    assert sent == []
    # the owner WAS told (by the run), so the episode is surfaced all the same
    assert surfaced == ["s-1"]


@pytest.mark.asyncio
async def test_the_echo_still_goes_out_when_the_run_stayed_quiet(monkeypatch):
    sent = []

    async def _tg(task_agent, job, final, deliver_target):
        sent.append(final)
        return "sent"

    monkeypatch.setattr(delivery, "_deliver_telegram", _tg)
    monkeypatch.setattr(delivery, "_event_log_for_read", lambda: _Log([_row("s-other")]))
    monkeypatch.setattr(delivery, "_mark_surfaced", lambda sid, uid=None: None)
    out = await delivery.deliver_result_ex(object(), _Job(), "Weekly synthesis complete.",
                                           target="telegram", session_id="s-1")
    assert out == "sent"
    assert sent == ["Weekly synthesis complete."]


def test_already_told_is_its_own_class_not_a_failure():
    assert delivery.delivery_outcome("x", "already_told") == "already_told"
