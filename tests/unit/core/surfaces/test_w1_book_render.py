"""W1: /book shows what the identity gate reads, and says what the verdict does.

* the reconcile ``collisions`` warning reaches the CLI/Telegram book (it was
  dropped: ``_ISSUE_SECTIONS`` never named it);
* ``open_positions`` rows the ledger does not list render as "tracked by the
  rail", with their lifecycle and the owner's /writeoff | /unquarantine;
* a DISAGREEMENT no longer promises "I will not trade" — no trade verb reads it.
"""
import types

from core.surfaces.inbox_render import BOOK_DISAGREEMENT_NOTE, render_book
from tools.defi.book import tracked_rows

FAKE = "0x357a04366240aa3c9d916aa0f15c3033686c9007"


def _body(**kw):
    body = {"verdict": "disagreement", "checked_at": None,
            "chains": {"robinhood": {"verdict": "disagreement", "report": {
                "collisions": ["⚠ ONE SYMBOL, MORE THAN ONE CONTRACT: PNL"],
                "unexplained": ["PNL 0xabc — held"]}}},
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
    tracked = [{"symbol": "PNL", "chain": "robinhood", "address": FAKE, "amount": 1.0,
                "entry": 134.54, "lifecycle": "quarantined",
                "lifecycle_reason": "look-alike"}]
    rows = [{"symbol": "OLD", "address": "0xdef", "lifecycle": "written_off",
             "lifecycle_reason": "rug"}]
    text = " ".join(render_book(_body(tracked=tracked, rows=rows), width=200).split())
    assert "Tracked by the rail, not in my ledger" in text
    assert "cost $134.54" in text and f"/unquarantine robinhood {FAKE}" in text
    assert "Not open" in text and "written off (rug)" in text


def test_tracked_rows_skip_what_the_ledger_lists():
    rec = types.SimpleNamespace(symbol="PNL", chain="robinhood", address=FAKE, qty=2.0,
                                entry_usd=10.0, status="quarantined", status_reason="x")
    ledger = [types.SimpleNamespace(address=FAKE.upper().replace("0X", "0x"))]
    assert tracked_rows(ledger, {FAKE: rec}) == []
    [row] = tracked_rows([], {FAKE: rec})
    assert row["lifecycle"] == "quarantined" and row["entry"] == 10.0
