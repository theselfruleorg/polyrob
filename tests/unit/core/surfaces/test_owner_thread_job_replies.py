"""C5 / C6 / B7 (2026-10-13 memory/coherency review): what a job told the owner,
and what the owner answered in PLAIN text, reach the job's next run."""
import pytest

from core.surfaces import owner_thread as ot


@pytest.fixture
def store(tmp_path):
    return ot.OwnerThreadStore(str(tmp_path / "owner_thread.db"))


def test_a_plain_reply_right_after_a_job_message_joins_the_rail_slice(store):
    store.record("out", "rob", "DM report: 3 offers waiting", via="telegram",
                 rail_id="cron:dm", source="cron", now=1000.0)
    store.record("in", "rob", "abandon all these offers", via="telegram", now=1100.0)
    rows = store.rail_rows("rob", rail_id="cron:dm", limit=6)
    assert [r["body"] for r in rows] == ["DM report: 3 offers waiting",
                                         "abandon all these offers"]
    rendered = ot.render_block(rows, kind="rail")
    assert "plain reply, right after your message above" in rendered


def test_the_newest_plain_reply_survives_a_busy_thread(store):
    """An older rail message with many chat lines after it must not crowd out
    the plain reply to the NEWEST rail message (one LIMIT over the whole span)."""
    store.record("out", "rob", "day 1 report", via="telegram", rail_id="cron:dm", now=1000.0)
    t = 1100.0
    for i in range(60):   # a busy day of ordinary chat after the first report
        store.record("in", "rob", f"chat {i}", via="telegram", now=t)
        store.record("out", "rob", f"answer {i}", via="telegram", source="reply", now=t + 1)
        t += 10
    store.record("out", "rob", "day 2 report", via="telegram", rail_id="cron:dm", now=t + 100)
    store.record("in", "rob", "stop posting that", via="telegram", now=t + 200)
    bodies = [r["body"] for r in store.rail_rows("rob", rail_id="cron:dm", limit=6)]
    assert bodies[-1] == "stop posting that"
    assert "day 2 report" in bodies


def test_a_plain_line_after_a_chat_reply_belongs_to_the_chat(store):
    store.record("out", "rob", "DM report", via="telegram", rail_id="cron:dm", now=1000.0)
    store.record("out", "rob", "chat answer", via="telegram", source="reply", now=1050.0)
    store.record("in", "rob", "thanks, what about tomorrow?", via="telegram", now=1100.0)
    bodies = [r["body"] for r in store.rail_rows("rob", rail_id="cron:dm", limit=6)]
    assert bodies == ["DM report"]


def test_a_late_plain_line_and_a_command_are_not_replies(store):
    store.record("out", "rob", "DM report", via="telegram", rail_id="cron:dm", now=1000.0)
    store.record("in", "rob", "/status", via="telegram", kind="command", now=1010.0)
    store.record("in", "rob", "much later", via="telegram",
                 now=1000.0 + ot.PLAIN_REPLY_WINDOW_S + 5)
    bodies = [r["body"] for r in store.rail_rows("rob", rail_id="cron:dm", limit=6)]
    assert bodies == ["DM report"]


def test_another_tenants_line_never_joins(store):
    store.record("out", "rob", "DM report", via="telegram", rail_id="cron:dm", now=1000.0)
    store.record("in", "eve", "stop it", via="telegram", now=1100.0)
    assert len(store.rail_rows("rob", rail_id="cron:dm", limit=6)) == 1


class _Container:
    def __init__(self, store):
        self._store = store

    def get_service(self, name):
        return self._store if name == ot.SERVICE_NAME else None


def test_a_run_send_without_a_rail_is_filed_under_its_job(store):
    """C6: the `message` tool's owner send carried no rail id."""
    from core.autonomy_marker import mark_autonomous
    mark_autonomous("sess-c6", None, cron_job_id="job-c6")
    assert ot.record_owner_out(_Container(store), "rob", "bought 0.1 ETH", via="telegram",
                               session_id="sess-c6", source="message_tool")
    rows = store.rail_rows("rob", rail_id="cron:job-c6", limit=6)
    assert [r["body"] for r in rows] == ["bought 0.1 ETH"]


def test_an_explicit_empty_rail_stays_empty(store):
    from core.autonomy_marker import mark_autonomous
    mark_autonomous("sess-c6b", None, cron_job_id="job-c6b")
    ot.record_owner_out(_Container(store), "rob", "x", via="telegram",
                        session_id="sess-c6b", source="reply", rail_id="")
    assert store.rail_rows("rob", rail_id="cron:job-c6b", limit=6) == []


@pytest.mark.asyncio
async def test_cron_delivery_carries_the_run_session(monkeypatch):
    """B7: the cron `deliver` send omitted session_id, so it was not the job's."""
    from cron import delivery
    from cron.jobs import CronJob
    seen = {}

    async def _rail(container, user_id, text, **kw):
        seen.update(kw)
        return "sent"

    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _rail)
    monkeypatch.setattr(delivery, "_owner_telegram", lambda *a, **k: "999")
    monkeypatch.setattr(delivery, "run_already_told_owner", lambda *a, **k: False)
    monkeypatch.setattr(delivery, "_mark_surfaced", lambda *a, **k: None)

    class _Agent:
        container = None
        config = None

    job = CronJob(id="j", task="t", schedule_spec="30m", user_id="rob", next_run_at=None,
                  one_shot=False, skip_memory=True, max_duration_seconds=60, payload={},
                  created_at=None)
    out = await delivery.deliver_result_ex(_Agent(), job, "report", target="telegram",
                                           session_id="sess-b7")
    assert out == "sent" and seen.get("session_id") == "sess-b7"
