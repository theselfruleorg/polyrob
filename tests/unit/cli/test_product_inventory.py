from cli.inventory import click_command_inventory, slash_command_inventory


def test_click_inventory_contains_product_aliases_and_tools():
    names = {row["name"] for row in click_command_inventory()}

    assert "cli tools" in names
    assert "cli tools list" in names
    assert "cli sessions" in names
    assert "cli models" in names


def test_click_inventory_contains_p1_commands():
    """P1 POLYROB Panel commands."""
    names = {row["name"] for row in click_command_inventory()}

    # P1.2 Goals CLI
    assert "cli goals" in names
    assert "cli goals list" in names

    # P1.4 Subagents CLI
    assert "cli subagents" in names
    assert "cli subagents info" in names

    # P1.4 Todos CLI
    assert "cli todos" in names
    assert "cli todos list" in names


def test_slash_inventory_contains_core_operator_commands():
    names = {row["name"] for row in slash_command_inventory()}

    assert {"status", "session", "sessions", "tools", "usage"}.issubset(names)


def test_slash_inventory_contains_p1_parity_commands():
    """P1.5 REPL slash parity commands."""
    names = {row["name"] for row in slash_command_inventory()}

    # P1.5 additions
    assert {"goals", "subagents", "todos", "logs", "export"}.issubset(names)
