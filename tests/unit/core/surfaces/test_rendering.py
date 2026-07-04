from core.surfaces.rendering import render_for_flavor


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
