"""057 WS-D — provenance stamping + the claim FORMAT guard (core/doc_claims.py)."""
import pytest

from core import doc_claims as dc


def test_stamp_only_new_and_changed_lines():
    old = "I live in Berlin\nI prefer short replies"
    new = "I live in Berlin\nI prefer short replies\nX posting is broken"
    out = dc.stamp_changed_lines(old, new, "room_read", "2026-09-19")
    lines = out.splitlines()
    assert lines[0] == "I live in Berlin"          # untouched
    assert lines[1] == "I prefer short replies"    # untouched
    assert lines[2] == "X posting is broken [from: room_read 2026-09-19]"


def test_changed_line_is_restamped_with_the_new_source():
    old = "X posting is broken [from: guess 2026-07-01]"
    new = "X posting works again"
    out = dc.stamp_changed_lines(old, new, "measured", "2026-09-19")
    assert out == "X posting works again [from: measured 2026-09-19]"


def test_unchanged_line_keeps_its_older_stamp_even_if_retyped_bare():
    old = "I live in Berlin [from: owner said 2026-07-01]"
    new = "I live in Berlin"          # agent re-typed the line without the stamp
    out = dc.stamp_changed_lines(old, new, "measured", "2026-09-19")
    assert out == "I live in Berlin [from: owner said 2026-07-01]"


def test_stamping_is_idempotent():
    old = ""
    new = "A new fact"
    once = dc.stamp_changed_lines(old, new, "measured", "2026-09-19")
    twice = dc.stamp_changed_lines(once, once, "measured", "2026-09-20")
    assert once == twice


def test_blank_lines_are_preserved_and_never_stamped():
    out = dc.stamp_changed_lines("", "a\n\nb", "s", "2026-09-19")
    assert out.splitlines()[1] == ""


def test_observed_at_normalisation_falls_back_to_today():
    assert dc.normalize_observed_at("2026-09-19") == "2026-09-19"
    assert dc.normalize_observed_at("2026-13-45") == dc.today_iso()
    assert dc.normalize_observed_at(None) == dc.today_iso()
    assert dc.normalize_observed_at("nonsense") == dc.today_iso()


def test_source_brackets_cannot_break_the_stamp_grammar():
    out = dc.stamp_changed_lines("", "a fact", "weird ] source [", "2026-09-19")
    assert dc.stamp_of(out) == "2026-09-19"


def test_a_new_line_keeps_the_stamp_its_author_wrote():
    """Provenance sticks to the sentence: re-stamping a stamped NEW line would
    re-date and re-attribute it to whoever wrote it down last (a promotion)."""
    old = ""
    new = "Owner prefers text. [from: owner said 2026-09-20]\nOwner is in Montreal."
    out = dc.stamp_changed_lines(old, new, "owner approved", "2026-09-21")
    lines = out.splitlines()
    assert lines[0] == "Owner prefers text. [from: owner said 2026-09-20]"
    assert lines[1] == "Owner is in Montreal. [from: owner approved 2026-09-21]"
