"""096 W6 — phone-safe formatting: what a Telegram owner reads on a phone."""
from core.surfaces.missed import format_notice_lines
from core.surfaces.rendering import (markdown_to_html, markdown_to_plain,
                                     render_for_flavor, split_for_flavor,
                                     split_markdown)


# --- markdown_to_html -----------------------------------------------------------

def test_numbered_list_keeps_its_numbers():
    out = markdown_to_html("1. one\n2) two\n  3. three")
    assert out == "1. one\n2. two\n  3. three"


def test_quote_becomes_blockquote_with_inline_formatting_inside():
    out = markdown_to_html("before\n> **a** quote\n> second\nafter")
    assert out == "before\n<blockquote><b>a</b> quote\nsecond</blockquote>\nafter"


def test_bold_cannot_straddle_a_quote_boundary():
    out = markdown_to_html("**open\n> quoted**")
    assert out.count("<b>") == out.count("</b>")
    assert "<blockquote>" in out


def test_rule_becomes_a_thin_line():
    out = markdown_to_html("a\n---\nb")
    assert "---" not in out and "──" in out


def test_narrow_table_reads_as_label_value_lines():
    text = "| key | value |\n|---|---|\n| cap | **$5** |\n| mode | auto |"
    out = markdown_to_html(text)
    assert "<pre>" not in out and "|" not in out
    assert out.split("\n") == ["<b>key</b>: value", "cap: <b>$5</b>", "mode: auto"]


def test_three_column_table_joins_the_rest_with_a_dot():
    out = markdown_to_html("| a | b | c |\n|---|---|---|\n| 1 | 2 | 3 |")
    assert out.split("\n")[-1] == "1: 2 · 3"


def test_wide_table_stays_pre():
    out = markdown_to_html("|a|b|c|d|\n|---|---|---|---|\n|1|2|3|4|")
    assert out.startswith("<pre>")


def test_other_flavors_are_untouched():
    text = "> q\n| a | b |\n|---|---|\n| 1 | 2 |\n---"
    assert render_for_flavor(text, "none", 4096) == [text]


# --- splitter --------------------------------------------------------------------

def _table(rows: int) -> str:
    return "intro\n| a | b | c | d |\n|---|---|---|---|\n" + "".join(
        f"| r{i} | v{i} | w{i} | z{i} |\n" for i in range(rows))


def test_table_split_across_a_cut_keeps_its_header():
    chunks = split_markdown(_table(40), 200)
    assert len(chunks) > 2
    for c in chunks[1:]:
        lines = c.strip("\n").split("\n")
        assert lines[0] == "| a | b | c | d |"
        assert lines[1] == "|---|---|---|---|"
    rendered = render_for_flavor(_table(40), "html", 200)
    assert all(c.startswith("<pre>") for c in rendered[1:])


def test_rendered_and_source_chunks_stay_aligned():
    text = _table(40)
    assert len(split_for_flavor(text, "html", 200)) == len(render_for_flavor(text, "html", 200))


def test_single_star_italic_is_not_cut_in_half():
    text = "word " * 10 + "*an italic phrase here* tail"
    for c in split_markdown(text, 70):
        assert c.count("*") % 2 == 0


# --- plain fallback ------------------------------------------------------------------

def test_plain_fallback_strips_markers_but_keeps_code():
    text = "# Head\n- **bold** *it* ~~gone~~ [docs](https://x.dev)\n```\nkeep **this**\n```"
    out = markdown_to_plain(text)
    assert "**bold**" not in out and "```" not in out and "#" not in out
    assert "bold" in out and "docs (https://x.dev)" in out
    assert "keep **this**" in out


def test_plain_fallback_empty():
    assert markdown_to_plain("") == ""


def test_telegram_fallback_sends_plain_text():
    import asyncio
    from surfaces.telegram.surface import TelegramSurface

    sent = []

    class _Bot:
        async def send_message(self, chat_id, text, parse_mode=None, **kw):
            if parse_mode:
                raise ValueError("can't parse entities")
            sent.append(text)
            return type("M", (), {"message_id": 1})()

    surf = TelegramSurface(_Bot())
    asyncio.run(surf.send_text("1", "**hi** there"))
    assert sent == ["hi there"]


# --- /missed --------------------------------------------------------------------------

def test_missed_keeps_line_breaks_bullets_and_fences():
    text = ("Summary\n- first point\n- second point that is rather long and wraps "
            "across the width limit\n```\nx = 1  # a code line that stays whole even past the width\n```")
    lines = format_notice_lines(0, "capped", text, width=50)
    joined = "\n".join(lines)
    assert lines[0].endswith("Summary")
    assert any(l.strip() == "- first point" for l in lines)
    assert any(l.strip().startswith("- second point") for l in lines)
    assert "x = 1  # a code line that stays whole even past the width" in joined
    assert sum(1 for l in lines if l.strip() == "```") == 2
