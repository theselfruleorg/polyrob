import pytest

from core.surfaces.rendering import markdown_to_html, render_for_flavor


def test_none_flavor_splits_on_limit_without_loss():
    chunks = render_for_flavor("abcdefghij", "none", 4)
    assert chunks == ["abcd", "efgh", "ij"]
    assert "".join(chunks) == "abcdefghij"


def test_markdown_v2_escapes_reserved_chars():
    out = render_for_flavor("a.b-c!", "markdown_v2", 4096)
    assert out == ["a\\.b\\-c\\!"]


def test_empty_text_yields_single_empty_chunk():
    assert render_for_flavor("", "none", 10) == [""]


def test_split_never_ends_chunk_on_odd_backslash_run():
    from core.surfaces.rendering import render_for_flavor
    text = "\\" * 50 + "x" * 50          # 50 backslashes then text
    chunks = render_for_flavor(text, "none", 7)
    assert "".join(chunks) == text       # byte-preservation
    for ch in chunks[:-1]:
        trailing = len(ch) - len(ch.rstrip("\\"))
        assert trailing % 2 == 0, f"chunk ends on odd backslash run: {ch!r}"


# --- html flavor: agent markdown -> Telegram HTML (the one converter) ---

def test_html_flavor_renders_bold_as_tag():
    assert render_for_flavor("**hi**", "html", 4096) == ["<b>hi</b>"]


def test_html_flavor_renders_italic_and_strike():
    assert render_for_flavor("*a* ~~b~~", "html", 4096) == ["<i>a</i> <s>b</s>"]


def test_html_flavor_escapes_html_special_chars():
    assert render_for_flavor("a < b & c", "html", 4096) == ["a &lt; b &amp; c"]


def test_html_flavor_renders_fenced_code_as_pre():
    out = render_for_flavor("```\nx = 1\n```", "html", 4096)[0]
    assert out.startswith("<pre>") and out.endswith("</pre>")
    assert "x = 1" in out


def test_html_flavor_leaves_markdown_inside_code_literal():
    out = render_for_flavor("`**not bold**`", "html", 4096)[0]
    assert out == "<code>**not bold**</code>"


def test_html_flavor_renders_link_as_anchor():
    out = render_for_flavor("[docs](https://x.dev/a?b=1&c=2)", "html", 4096)[0]
    assert out == '<a href="https://x.dev/a?b=1&amp;c=2">docs</a>'


def test_html_flavor_renders_heading_and_bullets():
    out = render_for_flavor("# Title\n- one\n- two", "html", 4096)[0]
    assert out == "<b>Title</b>\n• one\n• two"


def test_html_flavor_leaves_underscore_identifiers_alone():
    assert render_for_flavor("call my_var_name now", "html", 4096) == ["call my_var_name now"]


def test_html_flavor_splits_before_converting_so_tags_stay_balanced():
    chunks = render_for_flavor("**a**\n**b**", "html", 6)
    assert chunks == ["<b>a</b>", "<b>b</b>"]


def test_html_flavor_never_emphasises_inside_a_url():
    """A URL is not prose: '/_foo_/' must stay in the href, not become an <i> tag."""
    out = render_for_flavor("[t](https://x.dev/_foo_/bar)", "html", 4096)[0]
    assert out == '<a href="https://x.dev/_foo_/bar">t</a>'


def test_html_flavor_does_not_nest_bold_inside_a_heading():
    """A heading is already bold; a nested <b> is markup Telegram can reject."""
    assert render_for_flavor("# A **B** C", "html", 4096) == ["<b>A B C</b>"]


def test_html_flavor_survives_a_null_byte_in_the_source():
    """The converter stashes code spans behind \x00 markers; a \x00 already in the
    source must not index into that stash."""
    assert render_for_flavor("a\x000\x00b", "html", 4096) == ["a\x000\x00b"]


# --- one splitter / one escaper: guard against a second implementation drifting back ---

def test_surface_split_message_is_the_shared_splitter():
    """Every surface must chunk through core.surfaces.rendering.split_text, so a long
    reply breaks at the same (newline-preferring) boundary everywhere."""
    from core.surfaces.surface import split_message
    from core.surfaces.rendering import split_text
    text = "aaa\nbbb"
    assert split_message(text, 5) == split_text(text, 5) == ["aaa", "\nbbb"]


def test_legacy_duplicate_markdown_modules_are_gone():
    """utils/markdown_utils.py + utils/message_utils.py were a second escaper and a
    second splitter. core.surfaces.rendering is the only home."""
    import importlib
    for name in ("utils.markdown_utils", "utils.message_utils"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(name)


def test_html_flavor_keeps_every_streamed_prefix_balanced():
    """Incremental streaming (TELEGRAM_INCREMENTAL_STREAM) re-renders a GROWING prefix
    into editMessageText. A half-open tag mid-stream would 400 the edit, so the
    converter must only ever emit complete pairs."""
    from html.parser import HTMLParser
    from core.surfaces.rendering import markdown_to_html

    class _Balance(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack, self.ok = [], True

        def handle_starttag(self, tag, attrs):
            self.stack.append(tag)

        def handle_endtag(self, tag):
            if not self.stack or self.stack.pop() != tag:
                self.ok = False

    full = "Here is **bold** and `code` and [a link](https://x.dev/_a_/b) and ```py\nx=1\n``` done. 5 < 6 & 7 > 2"
    for i in range(1, len(full) + 1):
        p = _Balance()
        p.feed(markdown_to_html(full[:i]))
        p.close()
        assert p.ok and not p.stack, f"unbalanced HTML at prefix {i}: {markdown_to_html(full[:i])!r}"


# --- 030 WS-D2: construct-aware splitting (L6), tables (L7a), UTF-16 (L7b) ---

class TestChunkStraddle:
    def test_fence_split_across_chunks_renders_pre_in_both(self):
        text = "intro line here padding xx\n```python\nprint(1)\nprint(2)\n```\ndone"
        chunks = render_for_flavor(text, "html", 30)
        joined = "\n".join(chunks)
        assert "```" not in joined  # never a literal fence in the output
        assert sum(c.count("<pre>") for c in chunks) >= 1
        for c in chunks:
            assert c.count("<pre>") == c.count("</pre>")

    def test_giant_fence_is_closed_and_reopened(self):
        body = "\n".join(f"line_{i} = {i}" for i in range(40))
        text = f"```python\n{body}\n```"
        chunks = render_for_flavor(text, "html", 120)
        assert len(chunks) > 1
        for c in chunks:
            assert "```" not in c
            assert c.count("<pre>") == 1 and c.count("</pre>") == 1

    def test_bold_split_across_chunks_never_shows_asterisks(self):
        text = "aaaa bbbb cccc dddd **important thing** tail"
        chunks = render_for_flavor(text, "html", 20)
        joined = " ".join(chunks)
        assert "**" not in joined
        assert "<b>important thing</b>" in joined

    def test_inline_code_split_across_chunks(self):
        text = "run the command " + "pad " * 3 + "`polyrob doctor --flags` after"
        chunks = render_for_flavor(text, "html", 24)
        joined = " ".join(chunks)
        assert "`" not in joined
        assert "<code>polyrob doctor --flags</code>" in joined

    def test_link_split_across_chunks(self):
        # The limit (40) holds the whole link (34 chars) — the cut must move
        # before it. A link LONGER than the limit itself is a hard-cut by
        # necessity and out of contract.
        text = "see docs " + "pad " * 3 + "[the guide](https://example.com/a) end"
        chunks = render_for_flavor(text, "html", 40)
        joined = " ".join(chunks)
        assert "](" not in joined
        assert '<a href="https://example.com/a">the guide</a>' in joined

    def test_short_text_is_byte_identical_to_legacy(self):
        text = "hello **world** `code` [x](https://e.co) plain"
        assert render_for_flavor(text, "html", 4096) == [markdown_to_html(text)]


class TestTables:
    def test_markdown_table_degrades_to_pre(self):
        text = "| a | b |\n|---|---|\n| 1 | 2 |"
        out = markdown_to_html(text)
        assert out.startswith("<pre>")
        assert "| a | b |" in out

    def test_table_inside_prose(self):
        text = "before\n\n| h1 | h2 |\n| --- | --- |\n| x | y |\n\nafter"
        out = markdown_to_html(text)
        assert "<pre>" in out and "before" in out and "after" in out

    def test_pipes_without_separator_are_not_a_table(self):
        text = "a | b | c"
        assert "<pre>" not in markdown_to_html(text)


class TestUtf16Limits:
    def test_emoji_dense_chunks_fit_in_utf16_units(self):
        text = ("🚀" * 30 + " word ") * 10
        chunks = render_for_flavor(text, "html", 64)

        def u16(s):
            return len(s) + sum(1 for ch in s if ord(ch) > 0xFFFF)

        for c in chunks:
            assert u16(c) <= 64
        # nothing lost
        assert "".join(chunks).count("🚀") == text.count("🚀")
