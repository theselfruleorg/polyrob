"""Tests for P1.5 REPL slash parity commands."""

from cli.ui.commands import build_default_registry


def test_slash_goals_registered():
    """Test that /goals slash command is registered."""
    reg = build_default_registry()
    names = {c.name for c in reg.commands()}
    assert "goals" in names


def test_slash_subagents_registered():
    """Test that /subagents slash command is registered."""
    reg = build_default_registry()
    names = {c.name for c in reg.commands()}
    assert "subagents" in names


def test_slash_todos_registered():
    """Test that /todos slash command is registered."""
    reg = build_default_registry()
    names = {c.name for c in reg.commands()}
    assert "todos" in names


def test_slash_logs_registered():
    """Test that /logs slash command is registered."""
    reg = build_default_registry()
    names = {c.name for c in reg.commands()}
    assert "logs" in names


def test_slash_export_registered():
    """Test that /export slash command is registered."""
    reg = build_default_registry()
    names = {c.name for c in reg.commands()}
    assert "export" in names


def test_slash_inventory_includes_all_p1_commands():
    """Test that slash inventory includes all P1 commands."""
    reg = build_default_registry()
    names = {c.name for c in reg.commands()}

    # Core commands
    assert "help" in names
    assert "status" in names
    assert "usage" in names
    assert "tools" in names

    # P1 additions
    assert "goals" in names
    assert "subagents" in names
    assert "todos" in names
    assert "logs" in names
    assert "export" in names
