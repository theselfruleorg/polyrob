"""057 WS-D — the foundation block's age header (core/doc_age.py)."""
from datetime import date

from core.doc_age import measure_doc_age, render_age_header, stale_lines_note

NOW = date(2026, 9, 20)


def test_counts_dated_undated_and_stale():
    text = ("fresh [from: measured 2026-09-19]\n"
            "old [from: owner said 2026-01-01]\n"
            "undated line\n"
            "\n")
    age = measure_doc_age(text, now=NOW)
    assert (age.total, age.dated, age.undated, age.stale) == (3, 2, 1, 1)
    assert age.newest == "2026-09-19"
    assert age.oldest == "2026-01-01"


def test_header_names_last_write_and_undated_and_holds_as_days_pass():
    text = "a [from: m 2026-09-19]\nb [from: m 2026-01-01]\nc"
    h = render_age_header("Owner facts", text, now=NOW)
    assert h.startswith("## Owner facts (")
    assert "last write 2026-09-19" in h
    assert "older than" not in h
    later = date(NOW.year + 2, NOW.month, NOW.day)
    assert render_age_header("Owner facts", text, now=later) == h
    assert "undated" in h


def test_the_stale_count_is_a_tail_note_per_dated_block():
    old = "a [from: m 2026-01-01]\nb [from: m 2026-09-19]"
    blocks = (("owner_rules", old), ("self_doc", old), ("soul", old))
    assert stale_lines_note(blocks, now=NOW) == (
        "Owner facts: 1 line(s) older than 30 days.\nSelf notes: 1 line(s) older than 30 days.")
    assert stale_lines_note((("owner_rules", "b [from: m 2026-09-19]"),), now=NOW) == ""


def test_all_undated_says_undated_not_fresh():
    h = render_age_header("Owner facts", "a\nb", now=NOW)
    assert "undated" in h
    assert "last write" not in h


def test_empty_doc_renders_the_bare_heading():
    assert render_age_header("Owner facts", "", now=NOW) == "## Owner facts"
    assert render_age_header("Owner facts", "\n \n", now=NOW) == "## Owner facts"


def test_never_raises_on_garbage():
    assert render_age_header("X", None, now=NOW) == "## X"
    assert measure_doc_age("a [from: m 2026-99-99]", now=NOW).dated == 0
