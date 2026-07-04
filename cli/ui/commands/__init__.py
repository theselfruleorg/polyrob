"""commands — slash-command registry package (split from the 1135-line module, D6).

Public surface is unchanged: ``from cli.ui.commands import ReplExit, CommandContext,
Command, CommandRegistry, SlashCompleter, build_completer, build_default_registry,
default_registry, reset_default_registry, autonomy_status_lines`` all still work.
"""

from __future__ import annotations

from cli.ui.commands.registry import (
    Command,
    CommandContext,
    CommandRegistry,
    Handler,
    HandlerResult,
    ReplExit,
    SlashCompleter,
    build_completer,
)
from cli.ui.commands.handlers import (
    autonomy_status_lines,
    build_default_registry,
    default_registry,
    reset_default_registry,
)

# Handlers imported for back-compat (a test imports _h_help directly).
from cli.ui.commands.handlers import _h_help  # noqa: F401

__all__ = [
    "Command",
    "CommandContext",
    "CommandRegistry",
    "ReplExit",
    "SlashCompleter",
    "autonomy_status_lines",
    "build_completer",
    "build_default_registry",
    "default_registry",
    "reset_default_registry",
]
