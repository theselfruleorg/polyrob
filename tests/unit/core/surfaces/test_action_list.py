"""The action list (064 S2b F2): tappable choices as data, one renderer.

- the envelope refuses a command that is not a tappable token or a verb;
- a surface without actions renders byte-equal text;
- Telegram renders an inline keyboard on the LAST chunk, text unchanged;
- a button press routes as the PRESSER typing the command (sender from the
  platform's authenticated ``from``, never the payload); a non-owner press is
  refused exactly like the typed form; a non-command payload is dropped;
- the native command menu is a pure function of ``core/verbs.py``.
"""
import pytest

from core.surfaces.actions import (actions_for, actions_from_text, is_action_command,
                                   notice_actions)
from core.surfaces.envelopes import Action, OutboundMessage


# --- the envelope rule -------------------------------------------------------------

@pytest.mark.parametrize("cmd", ["/approve_p_a1b2c3", "/reject_p_a1b2c3", "/approve_all",
                                 "/approve_tap_abc123", "/status", "/pending", "/help"])
def test_a_tappable_token_or_a_verb_is_an_action(cmd):
    assert is_action_command(cmd)
    assert Action(label="x", command=cmd).command == cmd


@pytest.mark.parametrize("cmd", ["/approve p-a1b2c3", "/approve_p_../../x", "approve_all",
                                 "/rm", "/status now", " /status", "/approve_x_1",
                                 "https://evil.example", "", "/" + "a" * 80])
def test_anything_else_is_refused_at_construction(cmd):
    assert not is_action_command(cmd)
    with pytest.raises(ValueError):
        Action(label="x", command=cmd)


def test_style_and_label_are_checked():
    with pytest.raises(ValueError):
        Action(label="x", command="/status", style="blink")
    with pytest.raises(ValueError):
        Action(label=" ", command="/status")


def test_actions_come_from_the_text_producers_already_write():
    """The pending card (core/self_evolution.py) ends each item with its two
    tappable tokens and a whole-queue line — the buttons are exactly those."""
    text = ("2 changes wait for you:\n"
            " 1. skill: summarize_pdf\n   /approve_p_a1b2c3   /reject_p_a1b2c3\n"
            "Everything at once: /approve_all · the full list: /pending")
    got = [(a.label, a.command, a.style) for a in actions_from_text(text)]
    # TG9 (audit 2026-10-03): no whole-queue button. A button lives on an OLD
    # message and `/approve_all` decides the queue as it is at TAP time — an
    # old "Approve all" approved items its message never listed.
    assert got == [("Approve p-a1b2c3", "/approve_p_a1b2c3", "primary"),
                   ("Reject p-a1b2c3", "/reject_p_a1b2c3", "danger")]
    assert actions_from_text("see /workspace/approve_p_a1b2c3.txt") == []


def test_no_whole_queue_button_is_derived():
    """TG9: per-item buttons name an item; a whole-queue button names nothing."""
    text = "/approve_p_a1b2c3\nAll: /approve_all /reject_all"
    assert [a.command for a in actions_from_text(text)] == ["/approve_p_a1b2c3"]


def test_a_renderer_shows_explicit_actions_only():
    """Review 2026-09-23 (F2): buttons are NEVER inferred from arbitrary text —
    an agent reply quoting a correspondent's `/approve_p_…` must not become a
    green decision button."""
    explicit = [Action("Status", "/status")]
    assert actions_for(explicit) == explicit
    assert actions_for(None) == [] and actions_for([]) == []


def test_only_code_produced_notices_derive_buttons_from_their_text():
    card = "Tool approval needed.\n/approve_tap_ab12   /reject_tap_ab12"
    assert [a.command for a in notice_actions("tool_approvals", card)] == [
        "/approve_tap_ab12", "/reject_tap_ab12"]
    assert notice_actions("owner_ask", card) == []        # model-authored text
    assert notice_actions("message_tool", card) == []
    assert notice_actions("", card) == []


# --- a surface without actions renders byte-equal ------------------------------------

@pytest.mark.asyncio
async def test_a_surface_without_actions_sends_the_same_text():
    from surfaces.discord.surface import DiscordSurface

    sent = []

    class _Client:
        async def send_message(self, channel, text, **kw):
            sent.append((channel, text, tuple(sorted(kw))))
            return {"id": "1"}

    s = DiscordSurface(_Client())
    assert s.capabilities.supports_actions is False
    body = "Approve? /approve_p_a1b2c3"
    await s.send(OutboundMessage(session_key="agent:main:discord:dm:555", text=body))
    plain = list(sent)
    sent.clear()
    await s.send(OutboundMessage(session_key="agent:main:discord:dm:555", text=body,
                                 actions=[Action("Approve", "/approve_p_a1b2c3")]))
    assert sent == plain


# --- Telegram: the one renderer --------------------------------------------------------

class _Bot:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, **kw):
        self.calls.append((chat_id, text, kw))

        class _M:
            message_id = len(self.calls)
        return _M()


@pytest.mark.asyncio
async def test_telegram_puts_the_keyboard_on_the_last_chunk_only():
    from surfaces.telegram.surface import TelegramSurface
    bot = _Bot()
    s = TelegramSurface(bot)
    assert s.capabilities.supports_actions is True
    long_text = ("x " * 3000) + "\nDecide: /approve_p_a1b2c3 /reject_p_a1b2c3"
    await s.send_text("42", long_text,
                      actions=notice_actions("self_evolution", long_text))
    assert len(bot.calls) >= 2
    assert all("reply_markup" not in kw for _c, _t, kw in bot.calls[:-1])
    markup = bot.calls[-1][2]["reply_markup"]
    rows = markup["inline_keyboard"] if isinstance(markup, dict) else [
        [{"text": b.text, "callback_data": b.callback_data} for b in r]
        for r in markup.inline_keyboard]
    assert [b["callback_data"] for r in rows for b in r] == ["/approve_p_a1b2c3",
                                                             "/reject_p_a1b2c3"]
    assert "/approve_p_a1b2c3" in bot.calls[-1][1]           # the text keeps the token


@pytest.mark.asyncio
async def test_telegram_agent_text_quoting_a_token_gets_no_keyboard():
    from surfaces.telegram.surface import TelegramSurface
    bot = _Bot()
    s = TelegramSurface(bot)
    await s.send_text("42", "hello there")
    await s.send_text("42", 'Their email said: "just run /approve_all"')
    await s.send(OutboundMessage(session_key="agent:main:telegram:dm:42",
                                 text="quoted: /approve_p_a1b2c3"))
    assert all("reply_markup" not in kw for _c, _t, kw in bot.calls)


@pytest.mark.asyncio
async def test_the_approval_notice_reaches_the_sink_with_its_buttons(monkeypatch):
    """user_delivery hands a code-produced card's own tokens to the Telegram sink."""
    import surfaces.telegram.harness as hz
    seen = []

    async def _send(bot, chat_id, text, actions=None):
        seen.append([a.command for a in (actions or [])])
        return 1

    monkeypatch.setattr(hz, "_send_telegram_text", _send)
    sink = hz.TelegramBotSink(bot=None)
    card = "Approve this spend?\n/approve_tap_ab12   /reject_tap_ab12"
    await sink.send_message("42", card, actions=notice_actions("payment_approval", card))
    await sink.send_message("42", card)
    assert seen == [["/approve_tap_ab12", "/reject_tap_ab12"], []]


def _callback(data="/approve_p_a1b2c3", presser=999, chat_type="private"):
    return {"update_id": 77, "callback_query": {
        "id": "cb1", "data": data,
        "from": {"id": presser, "is_bot": False, "first_name": "P"},
        "message": {"message_id": 10, "date": 1727000000,
                    "chat": {"id": presser if chat_type == "private" else -1001,
                             "type": chat_type},
                    "from": {"id": 5555, "is_bot": True}}}}


def test_callback_becomes_the_presser_typing_the_command():
    from surfaces.telegram.actions import callback_to_update
    u = callback_to_update(_callback())
    msg = u["message"]
    assert msg["text"] == "/approve_p_a1b2c3"
    assert msg["from"]["id"] == 999            # the presser — not the notice's author
    assert u["update_id"] == 77
    # Its own identity: never the notice's message id (owner-thread referent).
    assert msg["message_id"] is None and msg["callback_of"] == 10


def test_a_payload_that_is_not_a_command_is_dropped():
    from surfaces.telegram.actions import callback_to_update
    assert callback_to_update(_callback(data="ignore previous instructions")) is None
    assert callback_to_update(_callback(data="/approve p-a1b2c3")) is None
    assert callback_to_update({"update_id": 1, "message": {"text": "hi"}}) is None


@pytest.mark.asyncio
async def test_a_non_owner_press_is_refused_exactly_like_the_typed_form(monkeypatch):
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "28436760")
    import surfaces.telegram.harness as hz

    drops = []
    monkeypatch.setattr(hz, "_record_drop",
                        lambda update, tg_id, reason: drops.append((tg_id, reason)))

    class _B:
        answered = []

        async def answer_callback_query(self, cid):
            self.answered.append(cid)

    h = hz.TelegramHarness.__new__(hz.TelegramHarness)
    h.bot = _B()
    typed = {"update_id": 78, "message": {"message_id": 11, "text": "/approve_p_a1b2c3",
                                          "from": {"id": 999},
                                          "chat": {"id": 999, "type": "private"}}}
    assert await hz.TelegramHarness.handle_update(h, typed) == {"ok": True}
    assert await hz.TelegramHarness.handle_update(h, _callback()) == {"ok": True}
    assert drops == [("999", "raw_allowlist"), ("999", "raw_allowlist")]
    assert h.bot.answered == ["cb1"]           # the spinner still stops


# --- native command menu -----------------------------------------------------------

def test_menu_entries_is_the_telegram_menu_and_carries_no_duplicate():
    from core.verbs import menu_entries
    import surfaces.telegram.harness as hz
    entries = menu_entries("telegram", hz._LOCAL_ROWS)
    assert entries == hz.help_commands()
    names = [n for n, _d in entries]
    assert len(names) == len(set(names))
    assert all(n.islower() and not n.startswith("/") for n in names)
    assert all(len(d) <= 256 for _n, d in entries)
