"""Contract test for the one core-tier verb table (043 palette, Task H2).

The table in ``core.verbs`` is the SSOT both the REPL registry and the console
command palette read. This pins it against ``core.surfaces.dispatcher._COMMANDS``
(the membership set that decides which slash lines route as COMMAND) so the two
can never drift: every routable verb has a row, and every row is routable.
"""
import re

from core.surfaces.dispatcher import _COMMANDS
from core.verbs import (
    GROUP_ORDER,
    PALETTE_EXCLUDED,
    SEAT_LOCAL,
    VERB_TABLE,
    Verb,
    grouped,
    seat_verbs,
    verb_for,
)

# The two verbs the console drops from /help (webview/console_commands.py
# CONSOLE_HELP_EXCLUDED) and therefore from the palette / verb table.
_EXCLUDED = frozenset({"/task"})


def _routable() -> set[str]:
    """Every routed name that a palette / verb table must describe:
    ``_COMMANDS`` plus the verbs a pack contributed (067 P5a — the union the
    dispatcher itself routes, ``command_names``)."""
    from core.surfaces.dispatcher import command_names
    return set(command_names()) - _EXCLUDED


def test_excluded_matches_the_module_constant():
    # The exclusion the test enforces IS the one the module documents.
    assert PALETTE_EXCLUDED == _EXCLUDED


def test_group_order_is_the_ten_sections_in_render_order():
    assert GROUP_ORDER == (
        "talk",
        "needs you",
        "work",
        "money",
        "control",
        "remember",
        "look",
        "set up",
        "display",
        "leave",
    )


def test_every_routable_command_has_exactly_one_row():
    table_names = [v.name for v in VERB_TABLE]
    # No duplicate rows.
    assert len(table_names) == len(set(table_names)), "duplicate verb row"
    # Bijection with _COMMANDS (minus the two excluded and the seat-local
    # rows): no missing, no stale. A SEAT-LOCAL verb is deliberately absent
    # from `_COMMANDS` — it never arrives as an inbound line on any surface,
    # it is typed into one seat's own reader.
    assert set(table_names) - SEAT_LOCAL == _routable()


def test_a_seat_local_row_is_never_routable():
    """The other direction. A verb in `_COMMANDS` arrives from the wire and
    must be handled by every surface that routes it — so it cannot also claim
    to belong to one seat."""
    assert not (SEAT_LOCAL & set(_COMMANDS))


def test_seat_local_rows_name_a_real_seat():
    seats = {s for v in VERB_TABLE for s in v.seats}
    assert seats <= {"repl", "telegram", "console", "cli"}, (
        f"unknown seat name(s): {sorted(seats - {'repl', 'telegram', 'console', 'cli'})}")


def test_seat_verbs_filters_and_grouped_agrees():
    repl = {v.name for v in seat_verbs("repl")}
    telegram = {v.name for v in seat_verbs("telegram")}
    # Every universal verb is on both seats; a repl-only verb is on one.
    assert "/gates" in repl and "/gates" not in telegram
    assert "/meter" in repl and "/meter" not in telegram
    assert "/status" in repl and "/status" in telegram
    # `grouped(seat)` renders exactly `seat_verbs(seat)`.
    flat = {v.name for _, rows in grouped("telegram") for v in rows}
    assert flat == telegram


def test_the_excluded_verb_has_no_row():
    names = {v.name for v in VERB_TABLE}
    assert not (_EXCLUDED & names)


def test_every_row_is_well_formed():
    # Env-flag shape (UPPER_WITH_UNDERSCORE) — a "flag name" on a first screen.
    flag_re = re.compile(r"[A-Z][A-Z0-9]*_[A-Z0-9_]*")
    for v in VERB_TABLE:
        assert isinstance(v, Verb)
        assert v.name.startswith("/") and v.name == v.name.lower(), v.name
        assert v.group in GROUP_ORDER, f"{v.name}: group {v.group!r} not in GROUP_ORDER"
        # help: one non-empty line, no config-flag name, no CLI flag.
        assert v.help.strip(), f"{v.name}: empty help"
        assert "\n" not in v.help, f"{v.name}: help is not one line"
        assert "--" not in v.help, f"{v.name}: help names a CLI flag"
        assert not flag_re.search(v.help), f"{v.name}: help names a config flag"
        assert isinstance(v.aliases, tuple), f"{v.name}: aliases must be a tuple"


def test_aliases_never_collide_with_a_canonical_name():
    canonical = {v.name for v in VERB_TABLE}
    seen: set[str] = set()
    for v in VERB_TABLE:
        for alias in v.aliases:
            assert alias.startswith("/"), f"{v.name}: alias {alias!r} needs a slash"
            assert alias not in canonical, f"{v.name}: alias {alias!r} shadows a verb"
            assert alias not in seen, f"duplicate alias {alias!r}"
            seen.add(alias)


def test_verb_for_resolves_with_or_without_slash():
    assert verb_for("/inbox") is verb_for("inbox")
    assert verb_for("/inbox").group == "needs you"
    assert verb_for("/task") is None  # excluded
    assert verb_for("") is None
    assert verb_for("/nope") is None


def test_verb_for_resolves_an_alias():
    assert verb_for("/h") is verb_for("/help")
    assert verb_for("/?") is verb_for("/help")


def test_grouped_with_no_seat_covers_every_row_in_group_order():
    seen_groups = [g for g, _ in grouped()]
    # Groups appear in GROUP_ORDER order (empty groups omitted).
    assert seen_groups == [g for g in GROUP_ORDER if g in seen_groups]
    # Every row is reachable through exactly one group.
    flat = [v for _, rows in grouped() for v in rows]
    assert {v.name for v in flat} == {v.name for v in VERB_TABLE}
    assert len(flat) == len(VERB_TABLE)


def test_memory_and_self_are_owner_reads_on_every_seat():
    """113 W6 G-6: the phone reads memory and the identity docs too; neither
    runs inside a group room."""
    for name in ("/memory", "/self"):
        row = verb_for(name)
        assert row is not None and row.seats == () and row.room_refused
        assert row in seat_verbs("repl") and row in seat_verbs("telegram")


# ---------------------------------------------------------------------------
# proposal 125 S11: one verb table drives the owner gate
# ---------------------------------------------------------------------------

#: The verbs ANY principal may run (a person's own chats, help, the welcome).
_ANY_PRINCIPAL = frozenset({"/start", "/new", "/chats", "/chat", "/rename", "/close",
                            "/cancel", "/help"})


def test_owner_gate_is_derived():
    """The routed set and the owner-gated set are DERIVED from the verb table, so a
    new row is routed AND owner-gated by default (fail closed)."""
    from core.verbs import owner_gated_names, routed_core_names
    from surfaces.telegram.harness import _OWNER_ADMIN_COMMANDS

    assert set(_COMMANDS) == set(routed_core_names()) | {"/task"}
    assert set(_OWNER_ADMIN_COMMANDS) == set(owner_gated_names())
    assert set(routed_core_names()) - set(owner_gated_names()) == _ANY_PRINCIPAL
    assert {v.name for v in VERB_TABLE if v.any_principal} == _ANY_PRINCIPAL


def test_a_pack_row_never_enters_the_derived_core_sets():
    """A pack verb is routed and gated through ``routed_names()`` at call time; the
    import-time derivation must not depend on whether a pack loaded first."""
    from core.verbs import (owner_gated_names, register_verbs, routed_core_names,
                            unregister_verbs)
    row = Verb("/zzpacktest", "work", "A test verb", owner_only="test")
    register_verbs([row], source="pack:zztest")
    try:
        assert "/zzpacktest" not in routed_core_names()
        assert "/zzpacktest" not in owner_gated_names()
    finally:
        unregister_verbs("pack:zztest")
