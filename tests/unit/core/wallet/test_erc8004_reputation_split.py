"""115 W5 — the feedback clients that paid an order here are put apart.

``split_paying`` is pure: it matches on the lowercase EVM address, keeps the
registry's client order, and never matches a non-EVM string.
"""
import pytest

from core.wallet.erc8004_reputation import split_paying

C1, C2, C3 = "0x" + "c1" * 20, "0x" + "C2" * 20, "0x" + "c3" * 20


def test_a_client_that_paid_is_put_apart_case_blind():
    paying, others = split_paying((C1, C2, C3), frozenset({C2.lower()}))
    assert paying == (C2,)
    assert others == (C1, C3)


def test_the_paid_set_may_be_mixed_case_too():
    paying, _ = split_paying((C1,), {C1.upper().replace("0X", "0x")})
    assert paying == (C1,)


def test_no_client_paid():
    assert split_paying((C1, C2), frozenset()) == ((), (C1, C2))


def test_no_clients_at_all():
    assert split_paying((), frozenset({C1})) == ((), ())


def test_a_non_evm_paid_entry_never_matches():
    base58 = "7EcDhSYGxXyscszYEp35KHN8vvw3svAuLKTzXwCFLtV"
    assert split_paying((C1,), {base58, "c1" * 20}) == ((), (C1,))


def test_an_unreadable_paid_set_is_refused_not_read_as_none_paid():
    with pytest.raises(TypeError):
        split_paying((C1,), None)
