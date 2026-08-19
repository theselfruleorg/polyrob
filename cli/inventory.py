"""Generated product inventories for commands and slash commands."""

from __future__ import annotations

from typing import Any, Dict, List


def click_command_inventory() -> List[Dict[str, Any]]:
    """Introspect the Click command tree exposed by cli.polyrob."""
    from cli.polyrob import cli

    return _walk_click(cli)


def slash_command_inventory() -> List[Dict[str, Any]]:
    """Introspect the built-in REPL slash command registry."""
    from cli.ui.commands import build_default_registry

    rows = []
    for command in build_default_registry().commands():
        rows.append(
            {
                "name": command.name,
                "aliases": list(command.aliases),
                "usage": command.usage,
                "help": command.help,
            }
        )
    return rows


def _walk_click(command, prefix: str = "", display_name: str | None = None) -> List[Dict[str, Any]]:
    rows = []
    name = display_name or command.name or "polyrob"
    qualified = f"{prefix} {name}".strip() if prefix else name
    # list_commands/get_command are the public click API — they include the
    # lazily-registered subcommands (cli.polyrob._LAZY_SUBCOMMANDS), which the
    # raw .commands dict does not hold until first use.
    child_names = (
        sorted(command.list_commands(None))
        if hasattr(command, "list_commands")
        else sorted(getattr(command, "commands", {}))
    )
    rows.append(
        {
            "name": qualified,
            "help": (command.help or "").strip(),
            "aliases": [],
            "commands": child_names,
        }
    )
    for child_name in child_names:
        child = command.get_command(None, child_name)
        if child is not None:
            rows.extend(_walk_click(child, qualified, child_name))
    return rows
