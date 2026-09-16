"""Grouped ``/help`` + ``/help <verb>`` (043 A13/A24).

The REPL ``/help`` used to print one flat 55-row wall (27 rows past 80
columns) with no ``/help <verb>``. This pins the replacement: a ten-group
catalog derived from the registry's ``group`` field
(``docs/design/040/cli/help-grouped-80.txt`` is the specification), and
``/help <verb>`` answering from ``help``/``help_long``/``elsewhere``
(``docs/design/040/cli/help-verb-80.txt``).

Harness pattern mirrors ``tests/unit/cli/test_repl_owner_verbs.py``: a
recorder renderer + a partial ``CommandContext``, handlers invoked directly.
"""
from __future__ import annotations

import shutil

import pytest


class _Recorder:
    def __init__(self):
        self.lines = []

    def print_block(self, text, *, title="", style=""):
        self.lines.append(text)

    @property
    def text(self):
        return "\n".join(self.lines)


def _ctx(args=(), registry=None):
    from cli.ui.commands.registry import CommandContext

    rec = _Recorder()
    ctx = CommandContext(renderer=rec, user_id="local", args=list(args), registry=registry)
    return ctx, rec


@pytest.fixture()
def _columns_80(monkeypatch):
    """Pin the terminal width h_help.py reads via shutil.get_terminal_size."""
    import os

    monkeypatch.setattr(
        shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((80, 24))
    )


# ---------------------------------------------------------------------------
# Registry: group coverage
# ---------------------------------------------------------------------------


def test_no_registered_verb_has_group_other():
    from cli.ui.commands.handlers import build_default_registry

    reg = build_default_registry()
    offenders = [c.name for c in reg.commands() if c.group == "other"]
    assert not offenders, f"verb(s) left in the default 'other' group: {offenders}"


def test_group_order_matches_registry_group_order():
    from cli.ui.commands.registry import GROUP_ORDER

    assert GROUP_ORDER == (
        "talk", "needs you", "work", "money", "control",
        "remember", "look", "set up", "display", "leave",
    )


# ---------------------------------------------------------------------------
# Grouped /help (no argument)
# ---------------------------------------------------------------------------


def test_every_registered_verb_appears_exactly_once_grouped(_columns_80):
    from cli.ui.commands.handlers import build_default_registry
    from cli.ui.commands.h_help import _grouped_help

    reg = build_default_registry()
    out = _grouped_help(reg)
    # Restrict to the group-listing lines (leading-space rows) — the header
    # sentence ("/help <verb> tells you…") also contains the literal "/help"
    # token and must not be counted as a second occurrence of the /help row.
    group_lines = [l for l in out.split("\n") if l.startswith(" ")]
    tokens = " ".join(group_lines).split()
    for cmd in reg.commands():
        token = f"/{cmd.name}"
        occurrences = tokens.count(token)
        assert occurrences == 1, f"{token} appeared {occurrences} time(s) in grouped /help"


def test_grouped_help_no_line_over_80_at_columns_80(_columns_80):
    from cli.ui.commands.handlers import build_default_registry
    from cli.ui.commands.h_help import _grouped_help

    reg = build_default_registry()
    out = _grouped_help(reg)
    long_lines = [(i, l) for i, l in enumerate(out.split("\n")) if len(l) > 80]
    assert not long_lines, f"line(s) over 80 cols: {long_lines}"


def test_grouped_help_group_order(_columns_80):
    from cli.ui.commands.handlers import build_default_registry
    from cli.ui.commands.h_help import _grouped_help
    from cli.ui.commands.registry import GROUP_ORDER

    reg = build_default_registry()
    out = _grouped_help(reg)
    seen = [
        line.strip().split()[0]
        for line in out.split("\n")
        if line.startswith("  ") and not line.startswith("   ") and line.strip()
        and not line.strip().startswith("/")
    ]
    # `seen` is every group-header line's first word, in the order rendered.
    assert seen == [g.split()[0] for g in GROUP_ORDER]


def test_grouped_help_header_and_footer_verbatim(_columns_80):
    from cli.ui.commands.handlers import build_default_registry
    from cli.ui.commands.h_help import _grouped_help

    reg = build_default_registry()
    out = _grouped_help(reg)
    assert out.startswith("Commands. /help <verb> tells you what one of them does.\n\n")
    assert "you don't have to use a command" in out.lower()
    assert "and I'll use my own" in out


def test_h_help_dispatches_grouped_with_no_args(_columns_80):
    from cli.ui.commands.handlers import _h_help

    ctx, rec = _ctx()
    _h_help(ctx)
    assert "Commands. /help <verb>" in rec.text
    assert "/status" in rec.text and "/pause" in rec.text


# ---------------------------------------------------------------------------
# /help <verb>
# ---------------------------------------------------------------------------


def test_help_finance_has_three_parts(_columns_80):
    from cli.ui.commands.handlers import _h_help

    ctx, rec = _ctx(args=["finance"])
    _h_help(ctx)
    out = rec.text
    # Part 1: the base line (usage + one-line help).
    assert "/finance [days]" in out
    assert "Balance sheet" in out
    # Part 2: help_long elaboration.
    assert "Two ledgers, never summed" in out
    # Part 3: elsewhere pointer.
    assert "Elsewhere:" in out
    assert "Finance page" in out


def test_help_verb_alias_resolution(_columns_80):
    """`/help ?` resolves the `?` alias to the canonical `help` command."""
    from cli.ui.commands.handlers import _h_help

    ctx, rec = _ctx(args=["?"])
    _h_help(ctx)
    assert "/help" in rec.text
    assert "Show this help" in rec.text


def test_help_verb_without_leading_slash_and_with(_columns_80):
    from cli.ui.commands.handlers import _h_help

    ctx1, rec1 = _ctx(args=["pause"])
    _h_help(ctx1)
    ctx2, rec2 = _ctx(args=["/pause"])
    _h_help(ctx2)
    assert rec1.text == rec2.text
    assert "/pause" in rec1.text


def test_help_unknown_verb(_columns_80):
    from cli.ui.commands.handlers import _h_help

    ctx, rec = _ctx(args=["nosuchverb"])
    _h_help(ctx)
    assert "Unknown command: /nosuchverb" in rec.text
    assert "/help" in rec.text


@pytest.mark.parametrize(
    "verb",
    [
        "pause", "resume", "halt", "pending", "asks", "fulfill", "finance",
        "invoices", "settle", "goals", "cron", "status", "doctor", "missed",
        "config", "model",
    ],
)
def test_minimum_verbs_have_help_long(verb):
    from cli.ui.commands.handlers import build_default_registry

    reg = build_default_registry()
    cmd = reg.lookup(verb)
    assert cmd is not None, f"/{verb} not registered"
    assert (cmd.help_long or "").strip(), f"/{verb} has no help_long"


def test_pause_help_long_is_the_mockup_verbatim():
    """The /pause detail body is docs/design/040/cli/help-verb-80.txt,
    verbatim (043 A13 brief: 'keep the five lines as drawn')."""
    from cli.ui.commands.handlers import build_default_registry

    reg = build_default_registry()
    cmd = reg.lookup("pause")
    for line in (
        "everything",
        "only the money verbs",
        "only the work I start by myself",
        "only the messages I send you",
        "until 6 hours from now",
    ):
        assert line in cmd.help_long


def test_verb_help_no_line_over_80_at_columns_80(_columns_80):
    from cli.ui.commands.handlers import build_default_registry
    from cli.ui.commands.h_help import _verb_help, HELP_TEXT

    reg = build_default_registry()
    for name in HELP_TEXT:
        cmd = reg.lookup(name)
        out = _verb_help(cmd)
        long_lines = [l for l in out.split("\n") if len(l) > 80]
        assert not long_lines, f"/{name}: line(s) over 80 cols: {long_lines}"


def test_width_floors_at_60(monkeypatch):
    """A tiny/piped terminal never collapses the wrap below the 60-col floor."""
    import os

    from cli.ui.commands.h_help import _width

    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((10, 24)))
    assert _width() == 60


def test_help_kwargs_empty_for_unauthored_verb():
    from cli.ui.commands.h_help import help_kwargs

    kw = help_kwargs("apps")
    assert kw == {"help_long": "", "elsewhere": ""}
