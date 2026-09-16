"""An owner or admin can CHOOSE to buy an action, to show the rail working.

Free-by-default is right: someone who already holds the authority should not be
taxed for using it, and `_apply_free` exists for exactly that. But it also means
the person most likely to demonstrate the paid rail is the one person who can
never trigger it.

So the free path is opt-OUT, one word: `/ban 1h pay`. Deliberate, visible in the
room, and it never taxes genuine moderation.

⚠️ Paying buys the PAYMENT FLOW, never authority. Every target protection still
applies — an admin who pays still cannot touch the owner, another admin, or
himself. The word only changes who is billed.
"""
import pytest

from surfaces.telegram.group_ops import split_pay_flag


# --- parsing ---------------------------------------------------------------

@pytest.mark.parametrize("args,duration,pay", [
    (["1h"], "1h", False),
    (["1h", "pay"], "1h", True),
    (["pay", "1h"], "1h", True),
    (["pay"], "", True),
    ([], "", False),
    (["30m"], "30m", False),
])
def test_the_pay_word_is_recognised_in_either_position(args, duration, pay):
    rest, wants_pay = split_pay_flag(args)
    assert wants_pay is pay
    assert (rest[0] if rest else "") == duration


def test_the_word_is_case_insensitive():
    assert split_pay_flag(["1h", "PAY"])[1] is True


def test_it_is_removed_so_it_never_parses_as_a_duration():
    """⚠️ Left in place it would reach `parse_duration` and refuse the verb."""
    rest, _ = split_pay_flag(["pay", "1h"])
    assert "pay" not in [a.lower() for a in rest]


def test_an_unrelated_word_is_left_alone():
    rest, wants_pay = split_pay_flag(["1h", "please"])
    assert wants_pay is False
    assert rest == ["1h", "please"]


def test_only_a_whole_token_counts():
    """`payment` or `paypal` must not trip it."""
    for word in ("payment", "paypal", "repay"):
        assert split_pay_flag(["1h", word])[1] is False


# --- what paying does and does not buy -------------------------------------

def test_the_member_verb_grant_is_not_required_for_an_admin_who_pays():
    """⚠️ `chat.member_verbs` gates MEMBERS. An owner already holds the
    authority, so requiring the grant would block the demo in exactly the rooms
    that never granted the verb to members."""
    import inspect

    from surfaces.telegram import group_ops
    src = inspect.getsource(group_ops._moderation_reply)
    gate = src[src.index("_member_may_use"):]
    assert 'role not in ("owner", "admin")' in src, (
        "the member-verb gate still applies to an owner/admin who pays")


def test_paying_still_goes_through_the_full_offer_path():
    """Paying buys the payment FLOW, not authority — every target protection
    is re-run by `offer()`, which the free path also calls."""
    import inspect

    from surfaces.telegram import group_ops
    src = inspect.getsource(group_ops._moderation_reply)
    assert "_paid_offer" in src
    assert "wants_pay" in src
