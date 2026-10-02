"""Interface audit 2026-10-03 — the Telegram poll loop.

TG1: an ``edited_message`` carries a NEW ``update_id``, so it passed the dedup
and routed as a fresh message: editing an old ``/send … go`` moved money
again. Edits are dropped before routing, and the poll asks Telegram not to
send them at all.

TG4: flood control (``retry_after``) on getUpdates is honoured, not answered
with a flat 1 s retry.

TG5: the Telegram dedup window covers a restart after a long inline verb.
"""
import asyncio

import pytest

from surfaces.telegram.harness import (
    TelegramHarness, _ALLOWED_UPDATES, _TG_DEDUP_WINDOW_S, build_telegram_harness,
)


class _Bot:
    def __init__(self, harness_ref):
        self.kw = []
        self.sent = []
        self._h = harness_ref

    async def get_updates(self, **kw):
        self.kw.append(kw)
        self._h["h"]._running = False
        return []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


def test_poll_never_asks_for_edits():
    ref = {}
    h = object.__new__(TelegramHarness)
    h._running = True
    h.poll_timeout = 0
    h.bot = _Bot(ref)
    ref["h"] = h
    asyncio.run(h.run_polling())
    allowed = h.bot.kw[0].get("allowed_updates")
    assert allowed is not None
    assert "edited_message" not in allowed
    assert "edited_channel_post" not in allowed
    assert set(allowed) == set(_ALLOWED_UPDATES)
    assert "message" in allowed and "callback_query" in allowed


@pytest.mark.parametrize("field", ["edited_message", "edited_channel_post"])
def test_an_edit_is_dropped_before_routing(monkeypatch, field):
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "12345")
    routed = []

    async def _fake_process_update(*a, **kw):
        routed.append(a)
        return None

    monkeypatch.setattr("surfaces.telegram.inbound.process_update", _fake_process_update)
    h = object.__new__(TelegramHarness)
    h.bot = _Bot({"h": h})
    upd = {"update_id": 77, field: {
        "chat": {"id": 555, "type": "private"},
        "from": {"id": 12345}, "text": "/send 1 native to 0xabc on base go"}}
    out = asyncio.run(h.handle_update(upd))
    assert out == {"ok": True}
    assert routed == []
    assert h.bot.sent == []


def test_poll_honours_retry_after(monkeypatch):
    slept = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    class _Flood(Exception):
        retry_after = 7

    h = object.__new__(TelegramHarness)
    h._running = True
    h.poll_timeout = 0
    calls = {"n": 0}

    class _B:
        async def get_updates(self, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _Flood("Flood control exceeded")
            h._running = False
            return []

    h.bot = _B()
    asyncio.run(h.run_polling())
    assert slept and slept[0] == 7


def test_dedup_window_outlives_a_long_inline_verb(tmp_path):
    class _C:
        def __init__(self):
            self.s = {}

        def get_service(self, n):
            return self.s.get(n)

        def register_service(self, n, v):
            self.s[n] = v

    class _B:
        pass

    h = build_telegram_harness(_C(), object(), token="x", bot=_B(),
                               data_dir=str(tmp_path))
    assert h.dedup.window_seconds == _TG_DEDUP_WINDOW_S
    assert _TG_DEDUP_WINDOW_S >= 24 * 3600
