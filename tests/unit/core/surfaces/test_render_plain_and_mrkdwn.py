"""A surface that shows no markdown never gets `**raw**` markers: Signal reads
plain text, Slack and WhatsApp their own single-marker markup. A command such as
/card_<id>_ok is never touched."""
import asyncio

from core.surfaces.rendering import markdown_to_mrkdwn, render_for_flavor

SRC = ("## Plan\n**Bold** and *soft* and ~~gone~~ `snake_case` [docs](https://x.io/a_b)\n"
       "- one\nTap /card_ab12_ok or /card_ab12_no. a_b_c\n```py\nx = **y**\n```")


def test_plain_strips_every_marker_and_keeps_commands():
    out = "\n".join(render_for_flavor(SRC, "plain", 4000))
    prose = out.split("x = ")[0]
    assert "**" not in prose and "`" not in prose and "##" not in prose and "~~" not in prose
    assert "`" not in out and "##" not in out and "~~" not in out
    assert "Bold and soft" in out and "docs (https://x.io/a_b)" in out
    assert "/card_ab12_ok" in out and "/card_ab12_no" in out and "a_b_c" in out
    assert "x = **y**" in out   # code is literal


def test_mrkdwn_uses_the_single_markers():
    out = markdown_to_mrkdwn(SRC)
    assert "*Plan*" in out and "*Bold*" in out and "_soft_" in out and "~gone~" in out
    assert "`snake_case`" in out and "docs (https://x.io/a_b)" in out
    assert "/card_ab12_ok" in out and "a_b_c" in out
    assert "```\nx = **y**\n```" in out


def test_none_still_passes_through_for_discord():
    assert render_for_flavor("**b**", "none", 100) == ["**b**"]


def test_long_text_splits_then_converts():
    chunks = render_for_flavor(("**word** " * 400).strip(), "mrkdwn", 500)
    assert len(chunks) > 1 and all(len(c) <= 500 for c in chunks)
    assert all("**" not in c for c in chunks)


def test_the_surfaces_declare_their_flavor():
    from surfaces.signal.surface import SignalSurface
    from surfaces.slack.surface import SlackSurface
    from surfaces.whatsapp.surface import WhatsAppSurface
    assert SignalSurface(None).capabilities.markdown_flavor == "plain"
    assert SlackSurface(None).capabilities.markdown_flavor == "mrkdwn"
    assert WhatsAppSurface(None).capabilities.markdown_flavor == "mrkdwn"


def test_a_harness_reply_and_a_sink_render_for_the_surface():
    from surfaces._shared import TextSink, split_for_surface
    assert split_for_surface("signal", "**hi** `x`", "plain") == ["hi x"]
    sent = []

    async def send(chat, text):
        sent.append(text)
    asyncio.run(TextSink(send, label="t", surface_id="slack", flavor="mrkdwn")
                .send_message("C1", "**hi**"))
    assert sent == ["*hi*"]


def test_slack_sends_mrkdwn_and_a_mrkdwn_button_section():
    from surfaces.slack.blocks import render_blocks
    from core.surfaces.envelopes import Action
    blocks = render_blocks("*hi* <&>", [Action(label="OK", command="/status")])
    assert blocks[0]["text"] == {"type": "mrkdwn", "text": "*hi* &lt;&amp;&gt;"}
