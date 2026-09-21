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


@pytest.mark.parametrize("line", [
    "X posting is broken",
    "The owner lacks permission",
    "Telegram has been disabled since July",
    "the rail no longer works",
    "posting does not work",
    "I cannot post to the channel",
    "the account is blocked",
    "delivery is unconfirmed",
])
def test_claim_lexicon_trips_without_a_source(line):
    assert dc.find_unsourced_claims("", line, None) == [line]


def test_a_source_answers_the_format_question():
    assert dc.find_unsourced_claims("", "X posting is broken", "room_read") == []
    # whitespace-only source does NOT count
    assert dc.find_unsourced_claims("", "X posting is broken", "   ") != []


def test_unchanged_claim_lines_do_not_trip_the_guard():
    old = "X posting is broken"
    assert dc.find_unsourced_claims(old, old + "\nI live in Berlin", None) == []


def test_a_plain_fact_is_not_a_claim():
    assert dc.find_unsourced_claims("", "I live in Berlin", None) == []
    # 'sincere' must not match 'since'
    assert dc.find_unsourced_claims("", "the owner is sincere", None) == []


def test_error_names_the_line_and_the_remedy():
    msg = dc.unsourced_claim_error(["X posting is broken", "y is blocked"])
    assert "X posting is broken" in msg
    assert "source=" in msg and "observed_at" in msg
    assert "1 more line" in msg
    assert "FORMAT check" in msg


def test_observed_at_normalisation_falls_back_to_today():
    assert dc.normalize_observed_at("2026-09-19") == "2026-09-19"
    assert dc.normalize_observed_at("2026-13-45") == dc.today_iso()
    assert dc.normalize_observed_at(None) == dc.today_iso()
    assert dc.normalize_observed_at("nonsense") == dc.today_iso()


def test_source_brackets_cannot_break_the_stamp_grammar():
    out = dc.stamp_changed_lines("", "a fact", "weird ] source [", "2026-09-19")
    assert dc.stamp_of(out) == "2026-09-19"


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("DOC_CLAIM_PROVENANCE_REQUIRED", raising=False)
    assert dc.claim_provenance_required() is False
    monkeypatch.setenv("DOC_CLAIM_PROVENANCE_REQUIRED", "true")
    assert dc.claim_provenance_required() is True


# --- 2026-09-20 07:33Z: a line that already carries a stamp IS sourced ------------------

def test_a_stamped_changed_line_has_answered_the_format_question():
    """Prod: the owner's /approve_all of a fully-stamped owner-facts draft was
    refused as 'unsourced' because promote() diffs against the ACTIVE doc with
    no source= — every differing line looked new, and the guard never looked at
    the stamp the line already carried."""
    old = "X posting is broken\n"
    new = ("X posting is broken since the API credit ran out [from: owner said 2026-09-20]\n"
           "The owner lacks permission on the channel [from: owner said 2026-09-20]\n")
    assert dc.find_unsourced_claims(old, new, None) == []
    mixed = new + "the rail no longer works\n"
    assert dc.find_unsourced_claims(old, mixed, None) == ["the rail no longer works"]


def test_a_new_line_keeps_the_stamp_its_author_wrote():
    """Provenance sticks to the sentence: re-stamping a stamped NEW line would
    re-date and re-attribute it to whoever wrote it down last (a promotion)."""
    old = ""
    new = "Owner prefers text. [from: owner said 2026-09-20]\nOwner is in Montreal."
    out = dc.stamp_changed_lines(old, new, "owner approved", "2026-09-21")
    lines = out.splitlines()
    assert lines[0] == "Owner prefers text. [from: owner said 2026-09-20]"
    assert lines[1] == "Owner is in Montreal. [from: owner approved 2026-09-21]"
