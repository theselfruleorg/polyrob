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
    VERB_TABLE,
    Verb,
    grouped,
    verb_for,
)

# The two verbs the console drops from /help (webview/console_commands.py
# CONSOLE_HELP_EXCLUDED) and therefore from the palette / verb table.
_EXCLUDED = frozenset({"/task", "/new"})


def _routable() -> set[str]:
    """Every _COMMANDS name that a palette / verb table must describe."""
    return {name for name in _COMMANDS} - _EXCLUDED


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
    # Bijection with _COMMANDS (minus the two excluded): no missing, no stale.
    assert set(table_names) == _routable()


def test_the_two_excluded_verbs_have_no_row():
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


def test_grouped_covers_every_row_in_group_order():
    seen_groups = [g for g, _ in grouped()]
    # Groups appear in GROUP_ORDER order (empty groups omitted).
    assert seen_groups == [g for g in GROUP_ORDER if g in seen_groups]
    # Every row is reachable through exactly one group.
    flat = [v for _, rows in grouped() for v in rows]
    assert {v.name for v in flat} == {v.name for v in VERB_TABLE}
    assert len(flat) == len(VERB_TABLE)
