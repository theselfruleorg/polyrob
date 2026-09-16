"""The recap belt, at the choke point instead of one renderer (C6 / plan T7).

Finding F3: `is_redundant_recap` lived in `cli/ui/dialog.py`, so Telegram,
webview, email and every other seat had nothing. Its lead-verb regex also
lacked `answered|informed|updated|summarised` and it capped at 200 chars — the
~600-char recap actually observed would have slipped through the ONE seat that
had the fix.

The latch (C1) is the braces; this is the belt. It must never eat a genuine
answer: when unsure, return False.
"""
from core.surfaces.recap import is_redundant_recap

BUBBLE = ("Yes — the deploy finished at 14:02 and both services came back healthy. "
          "The migration applied cleanly.")


# --- recaps that must be caught --------------------------------------------

def test_an_identical_repeat_is_a_recap():
    assert is_redundant_recap(BUBBLE, BUBBLE)


def test_the_widened_lead_verbs_are_caught():
    for lead in ("Answered the owner's two questions via Telegram.",
                 "Informed the user of the deploy status.",
                 "Updated the owner on the migration.",
                 "Summarised the outcome for the user.",
                 "Summarized the outcome for the user.",
                 "Notified the owner; no further action needed.",
                 "Responded to the greeting. Nothing else to do."):
        assert is_redundant_recap(lead, BUBBLE), lead


def test_a_long_recap_is_still_caught():
    """The observed leak was ~600 chars — well past the old 200-char ceiling."""
    long_recap = ("Answered the owner's two questions via Telegram: (1) the deploy "
                  "completed successfully at 14:02 UTC with both services returning "
                  "to a healthy state, and (2) the database migration applied "
                  "cleanly with no manual intervention required. " + "x" * 300)
    assert len(long_recap) > 200
    assert is_redundant_recap(long_recap, BUBBLE)


def test_a_restatement_of_the_bubble_is_a_recap():
    """No lead verb, but says nothing the bubble did not."""
    assert is_redundant_recap("The deploy finished at 14:02 and the migration "
                              "applied cleanly.", BUBBLE)


# --- genuine answers that must survive -------------------------------------

def test_a_real_answer_is_never_eaten():
    assert not is_redundant_recap("The capital of France is Paris.", BUBBLE)


def test_new_content_after_a_status_bubble_survives():
    assert not is_redundant_recap(
        "Here is the rollback command: `polyrob deploy --rollback 14712380`.", BUBBLE)


def test_a_long_genuine_answer_survives():
    poem = "\n".join(f"line {i} of an original poem about the sea" for i in range(40))
    assert not is_redundant_recap(poem, BUBBLE)


def test_no_bubble_means_nothing_to_be_redundant_with():
    assert not is_redundant_recap("Responded to the user.", "")
    assert not is_redundant_recap("Responded to the user.", None)


def test_empty_answer_is_not_a_recap():
    assert not is_redundant_recap("", BUBBLE)
    assert not is_redundant_recap(None, BUBBLE)


# --- back-compat -----------------------------------------------------------

def test_the_cli_import_site_still_works():
    from cli.ui import dialog
    assert dialog.is_redundant_recap(BUBBLE, BUBBLE)
