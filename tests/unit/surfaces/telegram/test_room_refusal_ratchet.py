"""D43: every MUTATING owner verb sits in one of the two room lists.

044 I10 established that the owner's money, host and control verbs must not
EXECUTE from inside a room: redirecting the reply to his DM was never enough,
because the action still ran, and the room is the one place a shoulder-surfer
is guaranteed. The list was then maintained by hand, and three mutating verbs
were missing from it — `/cron` (schedules a recurring run), `/goal` (cancels or
re-queues durable work) and `/fulfill` (declares an owner ask satisfied).

This derives the requirement instead of trusting the list to be remembered: a
verb that MUTATES is refused in a room, and a verb that does not is named as a
deliberate room-reachable read. A new verb therefore fails this test until
somebody decides which it is.
"""
import pytest

from core.surfaces.dispatcher import _COMMANDS
from surfaces.telegram.harness import (
    _OWNER_ADMIN_COMMANDS, _ROOM_REFUSED_COMMANDS,
)

#: Verbs that STAY reachable from a room. Each is either read-only, or scoped
#: to the room it is typed in, and each is answered in the owner's (or the
#: admin's) own DM. Adding a row here is a deliberate act, and the reason has
#: to survive being written down.
ROOM_REACHABLE = {
    # read-only
    "/status", "/help", "/goals", "/recap", "/journey", "/missed", "/book",
    "/inbox", "/asks", "/pending", "/allowlist", "/mode", "/avatar", "/kb",
    "/files", "/cwd", "/start",
    # scoped to THIS room, and the room is where they belong
    "/groups", "/mute", "/unmute", "/ban", "/unban", "/paid",
    # this room's own session lifecycle
    "/cancel", "/new", "/task",
}


def test_every_owner_verb_is_classified():
    """No verb may be un-classified: it is refused in a room, or it is named
    above as deliberately reachable from one."""
    unclassified = (set(_COMMANDS) - _ROOM_REFUSED_COMMANDS - ROOM_REACHABLE)
    assert unclassified == set(), (
        "these verbs are neither refused in a room nor declared room-reachable "
        f"— decide which, in harness._ROOM_REFUSED_COMMANDS or here: "
        f"{sorted(unclassified)}")


def test_the_two_lists_never_overlap():
    assert not (_ROOM_REFUSED_COMMANDS & ROOM_REACHABLE)


def test_every_refused_verb_is_routable():
    """A refusal for a verb that cannot route is dead code — and reads, to the
    next person, as protection that is not there."""
    assert _ROOM_REFUSED_COMMANDS <= set(_COMMANDS)


@pytest.mark.parametrize("verb", ["/cron", "/goal", "/fulfill"])
def test_the_three_mutating_verbs_the_list_was_missing(verb):
    """D43 by name, so a future edit that drops one fails loudly.

    Each MUTATES durable autonomous work from a chat where a member can read
    the ids off the screen and talk the owner into typing one.
    """
    assert verb in _ROOM_REFUSED_COMMANDS
    assert verb in _OWNER_ADMIN_COMMANDS


@pytest.mark.parametrize("verb", ["/claim", "/nft", "/dapp", "/identity"])
def test_the_new_money_verbs_are_refused_in_a_room(verb):
    """They join `/trade` and the rest for the same reason those did."""
    assert verb in _ROOM_REFUSED_COMMANDS


def test_contacts_is_refused_in_a_room():
    """A transcript with a third party is private correspondence."""
    assert "/contacts" in _ROOM_REFUSED_COMMANDS
