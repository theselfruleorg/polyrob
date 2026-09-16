"""The paid-effect catalog is CODE, never configuration (046 §2.2).

An agent that can author its own room-mutating action has granted itself
moderation power — the class 044's room deny-list and 031's pause record exist
to forbid. An owner prices a row; nobody adds an effect from a seat.
"""
import pytest

from core.surfaces import room_actions as ra


def test_mute_needs_the_restrict_right_and_a_duration():
    e = ra.effect("mute")
    assert e.telegram_right == "can_restrict_members"
    assert e.needs_duration is True
    assert e.max_duration_sec == 30 * 86400
    assert e.reversed_by == "unmute"


def test_unmute_needs_no_duration():
    assert ra.effect("unmute").needs_duration is False


def test_an_unknown_verb_resolves_to_none_and_never_to_a_default():
    assert ra.effect("delete_everything") is None
    assert ra.effect("") is None
    assert ra.effect(None) is None


def test_the_vocabulary_is_nameable_so_a_refusal_can_echo_it():
    assert set(ra.VERBS) == {"mute", "unmute", "ban", "unban"}


@pytest.mark.parametrize("spec,secs", [
    ("30m", 1800), ("2h", 7200), ("1d", 86400), ("7d", 604800), ("90m", 5400),
])
def test_parse_duration_accepts_the_grammar_group_admin_already_uses(spec, secs):
    """The SAME grammar `core/surfaces/group_admin.py` accepts, so an owner
    never has to learn a second one."""
    assert ra.parse_duration(spec) == secs


@pytest.mark.parametrize("bad", ["", "0m", "-1h", "forever", "2w", "2", "h",
                                 None, "1.5h"])
def test_parse_duration_refuses_anything_else(bad):
    assert ra.parse_duration(bad) is None


def test_every_effect_that_is_reversible_names_a_verb_in_the_catalog():
    """⚠️ A receipt offers the undo by name. A reversed_by that is not a catalog
    verb offers an undo nobody can buy."""
    for e in ra.EFFECTS.values():
        if e.reversed_by:
            assert e.reversed_by in ra.EFFECTS, (
                f"{e.verb} says it is reversed by {e.reversed_by!r}, which is "
                f"not a catalog verb")


def test_every_effect_names_a_telegram_right():
    """⚠️ The right is what gets checked BEFORE a mint. An effect with no named
    right would be sold without ever confirming we can perform it."""
    for e in ra.EFFECTS.values():
        assert e.telegram_right, f"{e.verb} names no Telegram permission"


def test_an_effect_that_needs_a_duration_has_a_nonzero_cap():
    for e in ra.EFFECTS.values():
        if e.needs_duration:
            assert e.max_duration_sec > 0, f"{e.verb} has no duration ceiling"


def test_the_catalog_cannot_be_extended_by_an_environment_variable(monkeypatch):
    """⚠️ The catalog is CODE. No env var and no chat key may add an effect."""
    import importlib
    monkeypatch.setenv("ROOM_ACTION_VERBS", "mute,delete_everything")
    monkeypatch.setenv("ROOM_ACTION_EFFECTS", "mute,delete_everything")
    reloaded = importlib.reload(ra)
    try:
        assert "delete_everything" not in reloaded.VERBS
        assert set(reloaded.VERBS) == {"mute", "unmute", "ban", "unban"}
    finally:
        importlib.reload(ra)


def test_every_verb_has_a_readable_past_tense():
    """⚠️ Deriving it ("ban" + "d") produced "band". The receipt is the one line
    the room reads to learn what it paid for."""
    from core.surfaces.room_actions import _PAST, VERBS
    assert set(_PAST) == set(VERBS)
    assert _PAST["ban"] == "banned" and _PAST["unban"] == "unbanned"


def test_the_receipt_reads_correctly_for_each_verb():
    from core.surfaces.room_actions import effect, receipt

    class _Row:
        target_name = "Sam"
        target_user_id = "9911"
        duration_sec = 3600

    for verb, expect in (("mute", "Sam is muted for 60 min"),
                         ("ban", "Sam is banned for 60 min")):
        row = _Row()
        row.verb = verb
        assert expect in receipt(row, effect(verb))
