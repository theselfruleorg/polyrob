"""D34: Telegram `/help` IS `core.verbs`, rendered — not a second grouping.

The help body used to be a hand-written wall of text: the same verbs, in
different sections, described in different words, with nothing pinning the two
together. The drift was not theoretical — `/missed` said one thing here and
another in the console palette, `/start` had no line at all (so `/help start`
answered "unknown command" for the verb the bot itself sends on first contact),
and `/groups use` shipped with no documentation on any seat.

The table owns WHAT a verb is and WHICH section it lives in. This module owns
only the two things a core-tier table may not carry: the argument grammar, and
this seat's own elaborations. These tests pin that division.
"""
import pytest

from core.surfaces.dispatcher import _COMMANDS
from core.verbs import GROUP_ORDER, PALETTE_EXCLUDED, VERB_TABLE, grouped
from surfaces.telegram.harness import (
    _DETAIL, _HELP_BODY, _LOCAL_ROWS, _SUBVERBS, _USAGE, _help_for,
    help_commands,
)


def _command_lines():
    """Every rendered line that starts a command, as ``(name, description)``."""
    out = []
    for line in _HELP_BODY.splitlines():
        if not line.startswith("/"):
            continue
        head, sep, desc = line.partition(" — ")
        if not sep:
            continue
        out.append((head.split(" ")[0], desc.strip()))
    return out


def test_every_command_line_description_comes_from_the_verb_table():
    """The contract. A description rendered here must be the table's own words,
    byte for byte — never a paraphrase this module invented."""
    by_name = {v.name: v.help for v in VERB_TABLE}   # every row, seat or not
    subverbs = {line.split(" ")[0]
                for lines in _SUBVERBS.values() for line in lines}
    local = {name for rows in _LOCAL_ROWS.values() for name, _, _ in rows}
    for name, desc in _command_lines():
        if name in local:
            continue          # /task and /new: PALETTE_EXCLUDED, worded here
        if name in subverbs and name not in by_name:
            continue          # a subverb line is not a _COMMANDS row
        assert name in by_name, f"{name} renders a help line with no table row"
        # A subverb line reuses its parent's NAME, so only the first (the verb's
        # own line) must match the table.
        if desc != by_name[name]:
            assert any(line.startswith(f"{name} ") and line.endswith(desc)
                       for lines in _SUBVERBS.values() for line in lines), (
                f"{name}: help line says {desc!r}, the verb table says "
                f"{by_name[name]!r} — the table is the SSOT")


def test_every_routable_verb_has_a_line():
    """A verb that routes but is not documented works and is invisible — in
    `/help` AND in the phone's "/" menu, which is parsed from this same body."""
    from core.verbs import SEAT_LOCAL
    rendered = {name for name, _ in _command_lines()}
    assert (set(_COMMANDS) - SEAT_LOCAL) - rendered == set()


def test_a_seat_local_verb_never_appears_here():
    """`/gates` and `/meter` are REPL-local. They never arrive as a Telegram
    message, so documenting them here would answer "unknown command" to
    anyone who tried."""
    from core.verbs import SEAT_LOCAL
    rendered = {name for name, _ in _command_lines()}
    assert not (rendered & SEAT_LOCAL)
    assert SEAT_LOCAL, "the seat-local set must not be empty, or this proves nothing"


def test_the_sections_are_seat_filtered():
    """The body renders `grouped("telegram")`, not the whole table."""
    from core.verbs import grouped as _grouped
    rendered = {name for name, _ in _command_lines()}
    telegram = {v.name for _, rows in _grouped("telegram") for v in rows}
    assert telegram <= rendered


def test_every_line_is_a_routable_verb_or_a_declared_subverb():
    """The other direction: no stale row for a verb that no longer exists."""
    subverbs = {line.split(" ")[0]
                for lines in _SUBVERBS.values() for line in lines}
    for name, _ in _command_lines():
        assert name in _COMMANDS or name in subverbs, f"stale help row {name}"


def test_the_sections_are_group_order():
    """The section titles ARE ``GROUP_ORDER``, in its order — not a second
    grouping with its own names."""
    titles = [ln for ln in _HELP_BODY.splitlines() if ln.startswith("— ")]
    expected = [f"— {g[:1].upper()}{g[1:]} —" for g, _ in grouped("telegram")]
    assert titles == expected
    assert [g for g, _ in grouped()] == [g for g in GROUP_ORDER
                                         if g in {v.group for v in VERB_TABLE}]


def test_usage_and_detail_maps_name_only_real_verbs():
    """A grammar or a detail line for a verb that does not exist is dead copy
    that reads as a promise."""
    known = {v.name for v in VERB_TABLE} | {
        name for rows in _LOCAL_ROWS.values() for name, _, _ in rows}
    for name in _USAGE:
        assert name in known, f"_USAGE names {name}, which is not a verb"
    for name in _DETAIL:
        assert name in known, f"_DETAIL names {name}, which is not a verb"


def test_the_two_excluded_verbs_are_still_documented_here():
    """`/task` and `/new` are dropped from the console palette, never from the
    chat seat — they are the two verbs a Telegram owner uses most."""
    rendered = {name for name, _ in _command_lines()}
    assert PALETTE_EXCLUDED <= rendered


@pytest.mark.parametrize("verb", ["/start", "/missed", "/groups", "/help"])
def test_help_for_answers_for_every_verb(verb):
    """D42: `/help start` was an "unknown command" reply for a verb the bot
    itself sends on first contact."""
    out = _help_for(verb)
    assert out.startswith(verb)
    assert "Unknown command" not in out


def test_help_for_resolves_an_alias():
    assert _help_for("h") == _help_for("/help")


def test_help_for_carries_the_detail_lines():
    out = _help_for("/pause")
    assert "trading" in out and "deploying" in out


def test_the_command_menu_is_parsed_from_the_same_body():
    """`help_commands()` feeds `setMyCommands`. It must see every verb and no
    duplicate name (Telegram rejects the whole call on a duplicate)."""
    names = [c for c, _ in help_commands()]
    assert len(names) == len(set(names))
    assert {f"/{n}" for n in names} >= set(_COMMANDS)


def test_groups_help_documents_the_use_verb():
    """E12: `/groups use` points a DM's `here` at a room. It shipped with no
    documentation on any seat, so the DM path to every room verb was invisible."""
    assert "use" in _USAGE["/groups"]
