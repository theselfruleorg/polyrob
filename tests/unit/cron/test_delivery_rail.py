"""§3.2 — cron telegram delivery rides the ONE user-delivery rail.

The rail contributes what cron delivery never had: content-hash dedup and
per-tenant caps (proposal 006's duplicate-spam class), while the cron layer
keeps its own gates ([SILENT], allowlist, proactive send-policy)."""
import asyncio

from cron.delivery import deliver_result
from cron.jobs import CronJob


def _job(**kw):
    base = dict(
        id="j1", task="report the news", schedule_spec="30m", user_id="777",
        next_run_at=None, one_shot=True, skip_memory=True, max_duration_seconds=180,
        payload=kw.pop("payload", {}), created_at=None,
    )
    base.update(kw)
    return CronJob(**base)


class _Sink:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True


class _Container:
    def __init__(self, sink):
        self._sink = sink

    def get_service(self, name):
        return self._sink if name == "telegram_sink" else None


class _TaskAgent:
    def __init__(self, sink):
        self.container = _Container(sink)


def test_cron_delivery_dedups_repeat_content():
    """The same digest text delivered twice within the dedup window sends once —
    the 45-min watermark job's re-narration spam becomes structurally impossible."""
    sink = _Sink()
    ta = _TaskAgent(sink)
    ok1 = asyncio.run(deliver_result(ta, _job(), "same digest text", target="telegram"))
    ok2 = asyncio.run(deliver_result(ta, _job(), "same digest text", target="telegram"))
    assert ok1 is True
    assert ok2 is False
    assert len(sink.sent) == 1


def test_cron_delivery_still_sends_fresh_content():
    sink = _Sink()
    ta = _TaskAgent(sink)
    assert asyncio.run(deliver_result(ta, _job(), "digest one", target="telegram")) is True
    assert asyncio.run(deliver_result(ta, _job(), "digest two", target="telegram")) is True
    assert [t for _, t in sink.sent] == ["digest one", "digest two"]


def test_cron_delivery_resolves_digit_uid_as_chat():
    sink = _Sink()
    ta = _TaskAgent(sink)
    asyncio.run(deliver_result(ta, _job(user_id="424242"), "hello", target="telegram"))
    assert sink.sent and sink.sent[0][0] == "424242"
