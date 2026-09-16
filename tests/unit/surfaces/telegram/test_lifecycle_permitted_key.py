"""`/cancel` and `/new` parse the session key; they never search it (043 T4).

`_lifecycle_permitted` let a non-owner DM sender cancel or restart THEIR OWN
session by asking whether `decision.session_key` CONTAINED `f":dm:{chat_id}:"`.
A session key is an address, not a haystack: its writer
(`core/surfaces/session_chat_registry.py::build_session_key`) joins
`agent:main:{surface}:{chat_type}:{chat_id}[:{user_id}][:thread:{thread_id}]`
on `:`, so those same bytes can appear in a LATER segment of a completely
different chat's key — and nothing checked WHO the key belonged to at all.

Parsed now: the surface and chat segments through the builder's own inverse, the
DM's user segment positionally. Fail-closed on anything else — a lifecycle verb
cancels a running task, so a maybe is a no.

⚠️ The surface is compared against THIS INBOUND's, never pinned to "telegram":
this harness is the SHARED inbound actor, so discord/slack/signal/x/email reach
these verbs too and a pinned string would deny them all.
"""
import pytest

from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from core.surfaces.session_chat_registry import build_session_key
from surfaces.telegram.harness import _lifecycle_permitted, _owns_dm_session_key
from surfaces.telegram.inbound import InboundResult

USER = "u_abc"


@pytest.fixture(autouse=True)
def _not_the_owner(monkeypatch):
    """Every case here is a NON-owner sender: the owner short-circuits to True
    before the key is ever looked at, which would hide the defect."""
    import surfaces.telegram.harness as h
    monkeypatch.setattr(h, "_is_admin_owner", lambda uid: False)


def _result(*, chat, user=USER, key, chat_type="dm", role=None, surface="telegram"):
    src = SessionSource(surface, str(chat), chat_type)
    identity = Identity(user_id=user, source=src, raw_user_id=str(chat),
                        chat_role=role)
    return InboundResult(
        inbound=InboundMessage(text="/cancel", identity=identity),
        decision=RouteDecision(RouteKind.COMMAND, key, command="/cancel"))


def test_a_sender_may_act_on_their_own_dm_session():
    key = build_session_key(SessionSource("telegram", "555", "dm"), USER)
    assert _lifecycle_permitted(_result(chat="555", key=key)) is True


def test_a_prefix_of_another_chat_id_never_matches():
    """`12` must not reach the session of `123` or `1234`."""
    for other in ("123", "1234", "120"):
        key = build_session_key(SessionSource("telegram", other, "dm"), USER)
        assert _lifecycle_permitted(_result(chat="12", key=key)) is False, other


def test_a_room_key_carrying_the_dm_bytes_never_matches():
    """The substring hole, built exactly: a GROUP key whose own segments spell
    `:dm:12:` further along. `build_session_key` produces it from a chat id that
    contains colons plus a thread — and the old check said yes."""
    src = SessionSource("telegram", "-100:dm:12", "group", thread_id="7")
    key = build_session_key(src, USER)
    assert ":dm:12:" in key, "the fixture must reproduce the substring hole"
    assert _lifecycle_permitted(_result(chat="12", key=key)) is False


def test_a_dm_key_belonging_to_another_user_never_matches():
    """The half nothing checked: WHO the key belongs to."""
    key = build_session_key(SessionSource("telegram", "555", "dm"), "u_someone_else")
    assert _lifecycle_permitted(_result(chat="555", key=key)) is False


def test_a_key_from_another_surface_never_matches():
    """Same chat id, same user, different SURFACE. The key names an address,
    and the surface is part of it."""
    key = build_session_key(SessionSource("email", "555", "dm"), USER)
    assert key == "agent:main:email:dm:555:u_abc"
    assert _lifecycle_permitted(_result(chat="555", key=key)) is False


def test_every_shared_actor_surface_still_reaches_its_own_session():
    """⚠️ The surface is compared against THIS INBOUND's, never pinned to the
    literal "telegram": this harness is the SHARED inbound actor, so discord,
    slack, signal, x and the email/webhook path run these verbs too. Pinning
    the string would deny `/cancel` to every non-telegram DM user."""
    for surface in ("telegram", "discord", "slack", "signal", "x", "email"):
        key = build_session_key(SessionSource(surface, "555", "dm"), USER)
        assert _lifecycle_permitted(
            _result(chat="555", key=key, surface=surface)) is True, surface


def test_a_dm_key_with_no_user_segment_is_denied():
    """`build_session_key` omits the user segment when it has no user_id — the
    key then names no owner, so it cannot name THIS one."""
    key = build_session_key(SessionSource("telegram", "555", "dm"), None)
    assert key == "agent:main:telegram:dm:555"
    assert _lifecycle_permitted(_result(chat="555", key=key)) is False


def test_a_threaded_dm_key_still_matches_its_own_sender():
    """A thread suffix is not a different session owner."""
    src = SessionSource("telegram", "555", "dm", thread_id="9")
    key = build_session_key(src, USER)
    assert _lifecycle_permitted(_result(chat="555", key=key)) is True


def test_a_group_sender_is_still_role_gated_not_key_gated():
    """044 T16 is untouched: in a room the ROLE decides, never the key."""
    key = build_session_key(SessionSource("telegram", "-100777", "group"), USER)
    assert _lifecycle_permitted(
        _result(chat="-100777", key=key, chat_type="group", role="admin")) is True
    assert _lifecycle_permitted(
        _result(chat="-100777", key=key, chat_type="group", role="member")) is False
    assert _lifecycle_permitted(
        _result(chat="-100777", key=key, chat_type="group")) is False


# ==========================================================================
# The parser itself — fail-closed on every unusable input
# ==========================================================================

@pytest.mark.parametrize("key", [
    None, "", "not a key", "agent:main", "agent:main:telegram:dm",
    "direct:telegram:555", "other:main:telegram:dm:555:u_abc",
])
def test_unusable_keys_are_denied_never_raise(key):
    assert _owns_dm_session_key(key, "555", USER, "telegram") is False


def test_a_missing_chat_user_or_surface_is_denied():
    key = build_session_key(SessionSource("telegram", "555", "dm"), USER)
    assert _owns_dm_session_key(key, None, USER, "telegram") is False
    assert _owns_dm_session_key(key, "", USER, "telegram") is False
    assert _owns_dm_session_key(key, "555", None, "telegram") is False
    assert _owns_dm_session_key(key, "555", "", "telegram") is False
    assert _owns_dm_session_key(key, "555", USER, None) is False
    assert _owns_dm_session_key(key, "555", USER, "") is False
    assert _owns_dm_session_key(key, "555", USER, "discord") is False


def test_numeric_and_string_ids_compare_the_same():
    """Telegram ids arrive as ints in some paths and strings in others."""
    key = build_session_key(SessionSource("telegram", "555", "dm"), USER)
    assert _owns_dm_session_key(key, 555, USER, "telegram") is True
