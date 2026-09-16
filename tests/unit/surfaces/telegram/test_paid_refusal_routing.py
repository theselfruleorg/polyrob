"""A member's refusal is answered WHERE THE MEMBER IS.

⚠️ Live incident, 2026-09-15, Playground Env. A plain member replied to his own
message with `/ban@tmachinroBot 1h`. The self-target guard fired correctly — and
the refusal went to his DM as a bare string, so the ROOM saw nothing at all.
Three people concluded the feature was broken; the ledger shows the owner
relaying "написал мне в личку" / "что типа я сам против себя покупаю".

The offer and the receipt are already posted in the room, addressed to the
member who asked. A refusal that names only HIM must go to the same place.

⚠️ The split is not "everything to the room". A refusal that describes the
PAYMENT RAIL's condition — it cannot settle this asset, the mint failed, the
owner is unresolvable, the bot is missing a Telegram right — tells a room
exactly when the rail is weak, which is information for somebody probing it.
Those stay owner-only.
"""
import pytest

from surfaces.telegram.group_ops import ROOM_FACING_REFUSALS, refusal_reply


class _Res:
    def __init__(self, reason_code, text="nope"):
        self.ok = False
        self.reason_code = reason_code
        self.text = text
        self.offer_id = ""
        self.invoice = None


# --- the incident ----------------------------------------------------------

def test_a_self_target_refusal_is_posted_in_the_room():
    """The exact case three people read as 'the feature is broken'."""
    out = refusal_reply(_Res("self_target", "You cannot buy this against yourself."))
    assert getattr(out, "to_room", False) is True
    assert "yourself" in out.text


@pytest.mark.parametrize("code", [
    "self_target", "protected_target", "payer_cap", "target_cap",
    "disabled", "paused", "unknown_verb", "bad_duration",
])
def test_member_facing_refusals_reach_the_room(code):
    out = refusal_reply(_Res(code))
    assert getattr(out, "to_room", False) is True, f"{code} did not reach the room"


# --- what must NOT be announced -------------------------------------------

@pytest.mark.parametrize("code", [
    "rail_unavailable", "mint_failed", "unpriceable", "owner_unknown",
    "missing_right", "unknown",
])
def test_rail_condition_refusals_stay_owner_only(code):
    """⚠️ Announcing these tells a room when the payment rail is weak."""
    out = refusal_reply(_Res(code))
    assert isinstance(out, str), f"{code} leaked to the room"


def test_an_unknown_future_code_stays_owner_only():
    """Fail-CLOSED: a reason code nobody has classified is not published."""
    out = refusal_reply(_Res("some_new_code_2027"))
    assert isinstance(out, str)


def test_the_two_buckets_do_not_overlap():
    assert "rail_unavailable" not in ROOM_FACING_REFUSALS
    assert "mint_failed" not in ROOM_FACING_REFUSALS
    assert "self_target" in ROOM_FACING_REFUSALS
