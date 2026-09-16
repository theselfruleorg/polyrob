"""035 P1-11 — the one-line change summary the owner gets after a rule change."""
from core.self_evolution import summarize_doc_change


def test_counts_added_and_removed_lines():
    s = summarize_doc_change("a\nb\n", "a\nc\nd\n")
    assert "+2" in s and "-1" in s


def test_names_a_first_added_line():
    s = summarize_doc_change("a\n", "a\nNO posting to the den\n")
    assert "NO posting to the den" in s


def test_created_from_nothing():
    s = summarize_doc_change("", "first rule\n")
    assert "+1" in s


def test_no_change_is_reported_as_such():
    assert "no change" in summarize_doc_change("a\nb\n", "a\nb\n").lower()


def test_never_raises_on_odd_input():
    assert isinstance(summarize_doc_change(None, None), str)
