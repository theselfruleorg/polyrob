"""085 W8 money L-1 / L-2: a proposal never skips the owner's `anyway` step,
and an infinite or not-a-number bound is not a bound."""
import pytest

from core.surfaces import cards

ADDR = "0x2FAa2566d98FC6eac6eD5F2DbA182Ffd2142f0e7"
QUOTE = ("transfer\n  RESULT: DRY RUN (simulation only)\n"
         f"To send it: /send 1 native to {ADDR} on ethereum max inf go")


def _store(tmp_path):
    return cards.CardStore(str(tmp_path / "c.db"))


def test_a_proposal_refuses_the_anyway_word(tmp_path):
    with pytest.raises(ValueError, match="anyway"):
        cards.proposal_card("u1", "/send", ["1", ADDR, "to", ADDR, "on", "base", "anyway"],
                            st=_store(tmp_path))
    with pytest.raises(ValueError, match="anyway"):
        cards.proposal_card("u1", "/send", ["1", ADDR, "to", ADDR, "ANYWAY", "on", "base"],
                            st=_store(tmp_path))


def test_a_plain_send_proposal_is_unchanged(tmp_path):
    card = cards.proposal_card("u1", "/send", ["1", "native", "to", ADDR, "on", "base"],
                               st=_store(tmp_path))
    assert card.kind == cards.KIND_PROPOSAL and card.line.endswith("on base")


@pytest.mark.parametrize("word", ["inf", "-inf", "nan", "Infinity", "NaN", "0", "-1", "x"])
def test_not_a_finite_positive_number(word):
    assert not cards._is_number(word)
    assert not cards._bound_only(["max", word])
    assert not cards._bound_only([word])


def test_a_finite_bound_still_passes():
    assert cards._is_number("2814.01")
    assert cards._bound_only(["max", "2814.01"])
    assert cards._bound_only(["max", "10", "min_raw", "123"])


def test_a_quote_line_with_an_infinite_max_makes_no_card():
    typed = ["1", "native", "to", ADDR, "on", "ethereum"]
    assert cards.confirm_line_in("/send", typed, QUOTE) is None
