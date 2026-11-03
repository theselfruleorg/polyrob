"""096 W4 (B1-B3, B6): the owner approves outbound speech it can READ.

A tweet (≤4000) or DM (≤10000) used to be approved from its first 160 chars,
an email from 60 chars of body, and ``/pending <id>`` showed only the one-line
summary although the queue stores the full card.
"""
from tools.controller.grant_card import OUTBOUND_INLINE_MAX_CHARS, render_grant_card


def test_3000_char_tweet_is_whole_on_the_pushed_card():
    text = "".join(chr(97 + i % 26) for i in range(3000))
    card = render_grant_card("twitter_post", {"text": text}, "tap-ab12")
    assert text in card
    assert "not shown here" not in card


def test_longer_text_names_hidden_count_and_the_tap_to_the_rest():
    text = "y" * (OUTBOUND_INLINE_MAX_CHARS + 500)
    card = render_grant_card("twitter_dm", {"recipient": "bob", "text": text}, "tap-ab12")
    assert text not in card
    assert f"+{len(text) - 1500} chars not shown here" in card
    assert "/pending_tap_ab12" in card


def test_full_content_shows_any_length():
    text = "z" * 10000
    card = render_grant_card("twitter_dm", {"recipient": "bob", "text": text}, "tap-ab12",
                             full_content=True)
    assert text in card


def test_email_body_is_not_displaced_by_subject():
    body = "Dear Ann,\n" + "word " * 200
    card = render_grant_card("send_email", {"to": "a@b.c", "subject": "Invoice",
                                            "body": body}, "tap-1")
    assert "• Subject: Invoice" in card
    assert body.strip() in card
    assert "For: Invoice" not in card


def test_html_body_shown_as_text():
    card = render_grant_card("send_email", {"to": "a@b.c", "html": "<p>Hi<br>you &amp; me</p>"},
                             "tap-1")
    assert "Hi\nyou & me" in card and "<p>" not in card


def test_html_link_shows_its_target_not_only_its_text():
    html = ('<p>Pay here: <a class="b" href="https://evil.example/x">https://bank.example</a>'
            " <a href='https://two.example'>two</a> <img src=https://pix.example/t.gif></p>")
    card = render_grant_card("send_email", {"to": "a@b.c", "html": html}, "tap-1")
    assert "https://bank.example [link: https://evil.example/x]" in card
    assert "two [link: https://two.example]" in card
    assert "[image: https://pix.example/t.gif]" in card


def test_fence_inside_text_cannot_close_the_block():
    card = render_grant_card("twitter_post", {"text": "a ``` b"}, "tap-1")
    assert card.count("```") == 2


def test_target_and_purpose_cuts_say_how_much_is_hidden():
    card = render_grant_card("t", {"to": "t" * 400, "purpose": "p" * 700}, "tap-1")
    assert "(+100 chars)" in card
    assert "(+100 chars)" in card.split("• For:")[1]


def test_review_of_tool_approval_returns_stored_card(monkeypatch):
    from surfaces.telegram import approval_ops
    from tools.controller.approval_queue import PendingSet
    item = {"kind": "tool_approval", "id": "tap-ab12", "preview": "twitter_post — text",
            "tool": "twitter_post", "card": "🔐 Approval needed: twitter_post\nWHOLE TEXT"}

    class C:
        user_id, data_dir, instance_id, board = "u", None, "agent1", None

    out = approval_ops._review_one(C(), PendingSet([item], []), "tap-ab12")
    assert "WHOLE TEXT" in out
    assert "/approve_" in out and "/reject_" in out


def test_pending_grant_card_is_the_full_view():
    from tools.controller.approval_queue import pending_grant_card
    text = "q" * 9000
    card = pending_grant_card({"tool_name": "twitter_dm",
                               "params": {"recipient": "bob", "text": text}}, "tap-1")
    assert text in card
