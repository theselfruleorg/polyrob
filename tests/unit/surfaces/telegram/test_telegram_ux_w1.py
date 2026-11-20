"""2026-11-20 Telegram UX review, wave 1: copy boxes, bare URLs, button colours,
the tap toast and no link previews."""
import asyncio

from core.surfaces.rendering import markdown_to_html

ADDR = "0xAbC0000000000000000000000000000000000001"
TX = "0x" + "ab" * 32


def test_an_address_a_hash_and_a_path_are_copy_boxes():
    out = markdown_to_html(f"to {ADDR} in {TX} wrote /home/u/report_v2.md")
    assert f"<code>{ADDR}</code>" in out and f"<code>{TX}</code>" in out
    assert "<code>/home/u/report_v2.md</code>" in out


def test_a_command_a_short_hex_and_a_slash_word_stay_text():
    out = markdown_to_html("send /status or 0xabc, and/or 1/2")
    assert "<code>" not in out


def test_backticked_values_are_not_wrapped_twice():
    out = markdown_to_html(f"`{ADDR}`")
    assert out == f"<code>{ADDR}</code>"


def test_a_bare_url_keeps_its_underscores():
    out = markdown_to_html("see https://example.com/_private_/y now")
    assert "https://example.com/_private_/y" in out and "<i>" not in out


def test_a_markdown_link_still_renders():
    out = markdown_to_html("[docs](https://x.io/a_b_c)")
    assert out == '<a href="https://x.io/a_b_c">docs</a>'


def test_button_style_reaches_the_keyboard():
    from core.surfaces.envelopes import Action
    from surfaces.telegram.actions import keyboard_rows
    rows = keyboard_rows([Action("Approve", "/status", "primary"),
                          Action("Reject", "/status", "danger"), Action("Read", "/status")])
    assert [b.get("style") for b in rows[0]] == ["primary", "danger", None]


def test_a_tap_answers_with_a_toast():
    from surfaces.telegram.actions import accept_callback

    class Bot:
        seen = None

        async def answer_callback_query(self, cid, text=None):
            Bot.seen = (cid, text)

    upd = {"update_id": 1, "callback_query": {"id": "c1", "data": "/status",
           "from": {"id": 1}, "message": {"message_id": 5, "chat": {"id": 1}}}}
    asyncio.run(accept_callback(Bot(), upd))
    assert Bot.seen == ("c1", "⏳ /status")


def test_the_bot_shows_no_link_preview():
    from surfaces.telegram.bot_api import make_bot
    bot = make_bot("123456:" + "A" * 35)
    assert bot.default.link_preview_is_disabled is True


def test_the_inbox_on_a_phone_has_no_margin_and_no_padding():
    from surfaces.telegram.owner_ops import phone_layout
    text = "  ! Spend $12 on Base                         waiting 2h\n    Why: a swap"
    assert phone_layout(text) == "! Spend $12 on Base · waiting 2h\n  Why: a swap"


def test_the_menu_lists_the_daily_verbs_first():
    import surfaces.telegram.harness as h
    names = [n for n, _d in h.help_commands()]
    assert names[:3] == ["status", "inbox", "new"] and "help" in names[:12]
