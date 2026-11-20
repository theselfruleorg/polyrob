"""/book with no wallet says what the console's Money > Book says."""
from core.copy import t as core_t
from core.surfaces.inbox_render import render_book
from webview.copy import t as console_t


def test_the_core_and_console_words_are_one():
    assert core_t("book.no_wallet") == console_t("money.book.empty_title")
    assert core_t("book.no_wallet_how") == console_t("money.book.empty_how")


def test_no_wallet_is_the_console_sentence_not_a_failed_read():
    text = render_book({"chains": {}, "rows": [], "verdict": "no_wallet",
                        "checked_at": 0.0, "error": None}, width=200)
    assert "The agent has no wallet yet." in text
    assert "polyrob wallet init" in text
    assert "could not read" not in text and "no chain at all" not in text
