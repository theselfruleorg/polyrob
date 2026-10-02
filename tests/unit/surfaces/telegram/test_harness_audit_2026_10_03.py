"""Interface audit 2026-10-03 — Telegram harness P3 items.

TG3: `/wallet balances` made blocking `urllib` RPCs ON the event loop.
TG11: turn tasks were bare `create_task` (no strong reference).
TG12: a failed command reply after an executed money verb was only logged.
"""
import asyncio
import threading

import pytest

from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces.telegram import harness
from surfaces.telegram.inbound import InboundResult


class _Cfg:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Container:
    def __init__(self, data_dir):
        self.config = _Cfg(data_dir)

    def get_service(self, name):
        return None


class _Agent:
    def __init__(self, data_dir):
        self.container = _Container(data_dir)


def _cmd(command, text, user="alice"):
    src = SessionSource("telegram", "555", "dm")
    inbound = InboundMessage(text=text, identity=Identity(user_id=user, source=src,
                                                          raw_user_id="555"))
    return InboundResult(inbound=inbound, decision=RouteDecision(
        RouteKind.COMMAND, "agent:main:telegram:dm:555:" + user, command=command))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    return tmp_path


def test_wallet_reads_run_off_the_event_loop(env, monkeypatch):
    seen = {}

    def _wallet_reply(args, user_id=None, data_dir=None):
        seen["thread"] = threading.get_ident()
        return "balances"

    monkeypatch.setattr("surfaces.telegram.owner_ops.wallet_reply", _wallet_reply)

    async def _run():
        seen["loop_thread"] = threading.get_ident()
        return await harness.act_on_inbound(_Agent(str(env)),
                                            _cmd("/wallet", "/wallet balances"))

    assert asyncio.run(_run()) == "balances"
    assert seen["thread"] != seen["loop_thread"]


def test_default_spawn_keeps_a_strong_reference():
    async def _run():
        done = asyncio.Event()

        async def _work():
            done.set()

        harness._spawn(_work(), None)
        assert harness._TURN_TASKS, "the spawned turn has no strong reference"
        await asyncio.wait_for(done.wait(), 1)
        await asyncio.sleep(0)

    asyncio.run(_run())


def test_a_failed_reply_after_a_money_verb_is_recorded_for_missed(monkeypatch):
    recorded = []
    monkeypatch.setattr("core.event_log.emit",
                        lambda kind, **kw: recorded.append((kind, kw)))
    harness._record_undelivered_reply("alice", "/send", "✅ SENT AND CONFIRMED tx 0xabc",
                                      RuntimeError("Bad Gateway"))
    assert recorded
    kind, kw = recorded[0]
    assert kind == "owner_notice"
    assert kw["attrs"]["text"].startswith("[undelivered")
    assert "SENT AND CONFIRMED" in kw["attrs"]["text"]
    assert kw["user_id"] == "alice"


def test_a_card_edit_renders_markdown_as_telegram_html(tmp_path):
    """TG10: the in-place card edit sent markdown with no parse_mode, so the
    owner saw raw backticks and asterisks where the first send had formatting."""
    from core.surfaces import cards
    from surfaces.telegram import card_ops

    st = cards.CardStore(str(tmp_path / "c.db"))
    cards.set_store(st)
    calls = []

    class _Bot:
        async def edit_message_text(self, **kw):
            calls.append(kw)

    try:
        card = cards.choice_card("u1", "Pick `one`", ["a", "b"], st=st)
        st.add_ref(card.card_id, "telegram", "42", "7")
        card_ops.install_editor(_Bot())
        editor = [fn for fn in cards._LISTENERS
                  if getattr(fn, "_telegram_card_editor", False)][0]
        asyncio.run(editor(card))
    finally:
        for fn in list(cards._LISTENERS):
            if getattr(fn, "_telegram_card_editor", False):
                cards.remove_listener(fn)
        cards.set_store(None)
    assert calls and calls[0]["parse_mode"] == "HTML"
    assert "<code>one</code>" in calls[0]["text"]
    assert "`" not in calls[0]["text"]


def test_plain_fallback_chunks_come_from_the_same_splitter():
    """OB19: when Telegram rejects the HTML, each chunk is resent as its plain
    SOURCE — which `split_text` cut at different places than the HTML chunks,
    so the fallback duplicated or dropped text."""
    from core.surfaces.rendering import render_for_flavor, split_for_flavor
    from surfaces.telegram.surface import TelegramSurface

    words = [f"w{i:04d}" for i in range(1400)]
    text = (" ".join(words[:600]) + "\n```\n" + "\n".join(words[600:900])
            + "\n```\n" + " ".join(words[900:]))
    limit = TelegramSurface(None).capabilities.max_message_bytes
    assert len(split_for_flavor(text, "html", limit)) == len(
        render_for_flavor(text, "html", limit))
    sent = []

    class _Bot:
        async def send_message(self, chat_id, body, parse_mode=None, **kw):
            if parse_mode:
                raise RuntimeError("Bad Request: can't parse entities")
            sent.append(body)
            return type("M", (), {"message_id": len(sent)})()

    asyncio.run(TelegramSurface(_Bot()).send_text("42", text))
    joined = " ".join(sent)
    for w in (words[0], words[599], words[600], words[899], words[900], words[-1]):
        assert joined.count(w) == 1, w
