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
    rows.append(
        {
            "name": qualified,
            "help": (command.help or "").strip(),
            "aliases": [],
            "commands": sorted(getattr(command, "commands", {}).keys()),
        }
    )
    for child_name in sorted(getattr(command, "commands", {})):
        rows.extend(_walk_click(command.commands[child_name], qualified, child_name))
    return rows
