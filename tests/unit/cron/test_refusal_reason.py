"""A refused cron run names the refusal (with its category) to the owner, not only
"refused or produced nothing" — the text used to reach only the run record's error."""
from cron.runner import refusal_reason


def test_a_refusal_halt_names_the_refusal_text():
    status = ("Session failed: Stopped: the model refused (category: cyber). "
              "Nothing was retried on another model.")
    assert refusal_reason(status) == status[len("Session failed: "):]


def test_the_refusal_text_is_cut_to_200_chars():
    assert len(refusal_reason("Session failed: Stopped: " + "x" * 500)) == 200


def test_an_empty_run_keeps_the_generic_words():
    assert refusal_reason(None) == "the run was refused or produced nothing"
    assert refusal_reason("Session failed: PERMANENT ERROR: 402") == (
        "the run was refused or produced nothing")
