"""The /send repeat guard keys on what the send MEANS (the seat's own parser: chain,
token, recipient), not on the literal words: the same payment in another word order,
another address case or a `0X` prefix is still a repeat. A repeat whose outcome is
unknown tells the agent to check /book first."""
from tests.unit.tools.controller.test_card_actions import _owner  # noqa: F401
from tests.unit.tools.controller.test_propose_send_already_paid import (A, TOKEN, _paid,
                                                                         _propose, _ran)


def test_another_word_order_is_the_same_payment(_owner):  # noqa: F811
    _paid(A)
    err = _propose(f"/send to {A} 311628 {TOKEN} on robinhood").error or ""
    assert "already paid" in err


def test_a_max_bound_before_the_amount_is_the_same_payment(_owner):  # noqa: F811
    _paid(A)
    err = _propose(f"/send max 40 311628 {TOKEN} to {A} on robinhood").error or ""
    assert "already paid" in err


def test_the_key_folds_the_address_prefix_and_case():
    from tools.controller.card_actions import _send_key
    lower = _send_key(["1", TOKEN, "to", A, "on", "base"])
    upper = _send_key(["1", "0X" + TOKEN[2:].upper(), "to", "0X" + A[2:].upper(),
                       "on", "BASE"])
    assert lower == upper == ("base", TOKEN.lower(), A.lower())
    assert _send_key(["1", "eth", "to", A, "on", "ethereum"]) == ("ethereum", "native",
                                                                  A.lower())
    assert _send_key(["1", "native", "to", A, "on", "base"]) != (
        _send_key(["1", "native", "to", A, "on", "ethereum"]))


def test_an_unknown_outcome_points_at_the_book(_owner):  # noqa: F811
    done = _ran(A, "transfer\n  RESULT: BROADCAST BUT NOT CONFIRMED within the timeout.")
    assert done.outcome == "unknown"
    err = _propose(f"/send 311628 {TOKEN} to {A} on robinhood").error or ""
    assert "if /book shows that it did not land, pass also_owed=true" in err.lower()

