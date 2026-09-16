"""core/book.py — the typed reconcile verdict (043 A34).

The console's /positions page used to regex the reconcile verb's prose to
decide green/red; the always-present footer text ("If it dis**agrees** with")
made the DISAGREEMENT-string match fire on every render, so UNVERIFIED (every
balance read failed) rendered as green "IN AGREEMENT". `verdict_from_report`
replaces that regex with a typed value derived from the report's own lists —
never inferred from prose.
"""
from core.book import BookVerdict, verdict_from_report


def test_unverified_is_not_clean():
    d = {"matched": ["WETH"], "unknown": ["BONK"], "mismatched": [], "unbacked": [],
         "unexplained": [], "unreadable_rows": [], "verdict": "UNVERIFIED"}
    assert verdict_from_report(d, ledger_found=True) is BookVerdict.UNVERIFIED


def test_no_ledger_is_its_own_state():
    assert verdict_from_report({}, ledger_found=False) is BookVerdict.NO_LEDGER


def test_disagreement_wins_over_unknown():
    d = {"unbacked": ["BOTS"], "unknown": ["X"], "verdict": "DISAGREEMENT"}
    assert verdict_from_report(d, ledger_found=True) is BookVerdict.DISAGREEMENT


def test_clean_when_every_list_empty():
    d = {"matched": ["WETH"], "mismatched": [], "unbacked": [], "unexplained": [],
         "unreadable_rows": [], "unknown": [], "verdict": "CLEAN"}
    assert verdict_from_report(d, ledger_found=True) is BookVerdict.CLEAN


def test_no_ledger_wins_even_over_disagreement_shaped_dict():
    # ledger_found is the gate that fires FIRST — a dict shaped like a
    # disagreement is irrelevant if there was never a ledger to compare.
    d = {"unbacked": ["BOTS"]}
    assert verdict_from_report(d, ledger_found=False) is BookVerdict.NO_LEDGER


def test_verdict_values_are_lowercase_strings():
    assert BookVerdict.CLEAN.value == "clean"
    assert BookVerdict.DISAGREEMENT.value == "disagreement"
    assert BookVerdict.UNVERIFIED.value == "unverified"
    assert BookVerdict.NO_LEDGER.value == "no_ledger"
