"""W1: /book shows what the identity gate reads, and says what the verdict does.

* the reconcile ``collisions`` warning reaches the CLI/Telegram book (it was
  dropped: ``_ISSUE_SECTIONS`` never named it);
* book rows that wait on an owner decision render with their lifecycle and
  the owner's /writeoff | /unquarantine;
* a DISAGREEMENT no longer promises "I will not trade" — no trade verb reads it.
"""
import types

from core.surfaces.inbox_render import BOOK_DISAGREEMENT_NOTE, render_book
from tools.defi.book import tracked_rows

FAKE = "0x3535353535353535353535353535353535353535"


def _body(**kw):
    body = {"verdict": "disagreement", "checked_at": None,
            "chains": {"robinhood": {"verdict": "disagreement", "report": {
                "collisions": ["⚠ ONE SYMBOL, MORE THAN ONE CONTRACT: TKN"],
                "unexplained": ["TKN 0xabc — held"]}}},
            "rows": [], "tracked": []}
    body.update(kw)
    return body


def test_collisions_render_first():
    text = render_book(_body())
    assert "ONE SYMBOL, MORE THAN ONE CONTRACT" in text
    assert text.index("ONE SYMBOL") < text.index("held on the chain")


def test_the_disagreement_note_is_the_true_behavior():
    text = render_book(_body())
    assert "will not trade" not in text
    assert "does not stop a trade" in " ".join(text.split())
    assert "does not stop a trade" in BOOK_DISAGREEMENT_NOTE


def test_tracked_rows_and_lifecycle_render_with_the_owner_verbs():
    tracked = [{"symbol": "TKN", "chain": "robinhood", "address": FAKE, "amount": 1.0,
                "entry": 134.54, "lifecycle": "quarantined",
                "lifecycle_reason": "look-alike"}]
    rows = [{"symbol": "OLD", "address": "0xdef", "lifecycle": "written_off",
             "lifecycle_reason": "rug"}]
    text = " ".join(render_book(_body(tracked=tracked, rows=rows), width=200).split())
    assert "Waiting on your decision" in text
    assert "cost $134.54" in text and f"/unquarantine robinhood {FAKE}" in text
    assert "Not open" in text and "written off (rug)" in text


def test_tracked_rows_are_the_rows_that_wait_on_a_decision():
    from core.book import BookRow
    def _row(state, lifecycle):
        return BookRow(symbol="TKN", address=FAKE, chain="robinhood", qty=2.0, usd=None,
                       state=state, entry=10.0, lifecycle=lifecycle)
    assert tracked_rows([_row("matched", "open")]) == []
    [q] = tracked_rows([_row("matched", "quarantined")])
    assert q["lifecycle"] == "quarantined" and q["entry"] == 10.0
    [u] = tracked_rows([_row("unbacked", "open")])
    assert u["lifecycle"] == "open" and u["chain"] == "robinhood"
