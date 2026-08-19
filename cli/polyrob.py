"""POLYROB CLI entry point.

Bare ``polyrob`` opens the chat REPL. ``polyrob --help`` lists every command,
grouped (Start here / Surfaces / Autonomy & work / Money / Inspect & admin —
027 WP6); first-run path: ``polyrob init`` → ``polyrob doctor`` → ``polyrob``.

Startup contract: importing this module must stay CHEAP (stdlib + click + core.version).
Subcommands are lazy-loaded via ``_LazyGroup`` — each command module is imported only
when that subcommand is actually invoked, so `polyrob` (the REPL), `polyrob version`,
etc. don't pay for the whole tools/LLM import pyramid. Guarded by
tests/test_import_layering.py; keep new command registrations in _LAZY_SUBCOMMANDS,
not as top-level imports.
"""

import importlib
import sys

import click

from core.version import get_version

VERSION = get_version()


# --- Lazy subcommand registry (P3: thin entry, now import-thin too) ---
# name -> "module:attr". Aliases map two names to one target (the loaded command
# object keeps its own .name, exactly like add_command(name=...) did).
_LAZY_SUBCOMMANDS = {
    "run": "cli.commands.run:run",
    "session": "cli.commands.session:session",
    "sessions": "cli.commands.session:session",  # product vocabulary alias
    "model": "cli.commands.model:model",
    "models": "cli.commands.model:model",  # product vocabulary alias
    "skills": "cli.commands.skills:skills",
    "skill": "cli.commands.skill_install:skill",
    "tools": "cli.commands.tools:tools",
    "init": "cli.commands.init:init_cmd",
    "config": "cli.commands.config:config",
    "auth": "cli.commands.auth:auth",
    "doctor": "cli.commands.doctor:doctor",
    "telegram": "cli.commands.telegram:telegram",
    "whatsapp": "cli.commands.whatsapp:whatsapp",
    "email": "cli.commands.email:email",
    "owner": "cli.commands.owner:owner",
    "kb": "cli.commands.kb:kb",
    "serve": "cli.commands.serve:serve",
    "dashboard": "cli.commands.dashboard:dashboard",
    "webgate": "cli.commands.dashboard:dashboard",  # alias
    "surface": "cli.commands.surface:surface",
    "gateway": "cli.commands.gateway:gateway",
    "goals": "cli.commands.goals:goals",
    "cron": "cli.commands.cron:cron",
    "subagents": "cli.commands.subagents:subagents",
    "todos": "cli.commands.todos:todos",
    "update": "cli.commands.update:update_cmd",
    "pfp": "cli.commands.pfp:pfp",
    "soul": "cli.commands.soul:soul",
    "journey": "cli.commands.journey:journey",
    "finance": "cli.commands.finance:finance",
    "wallet": "cli.commands.wallet:wallet_cmd",
    "datagen": "cli.commands.datagen:datagen",
    "knowledge": "cli.commands.knowledge:knowledge",
    "approvals": "cli.commands.approvals:approvals",
    "discord": "cli.commands.discord:discord",
    "slack": "cli.commands.slack:slack",
    "signal": "cli.commands.signal:signal",
    "x": "cli.commands.x:x",
    "x-account": "cli.commands.x_account:x_account",
}

# --- Help-surface layout (027 WP6) ---
# canonical name -> alias names. Aliases stay invocable but render on the
# canonical row ("session (alias: sessions)"), never as duplicate entries.
_COMMAND_ALIASES = {
    "session": ("sessions",),
    "model": ("models",),
    "dashboard": ("webgate",),
}
_ALIAS_NAMES = {alias for aliases in _COMMAND_ALIASES.values() for alias in aliases}

# Grouped --help: a first-run user needs "start here", not 40 flat rows.
# Anything unlisted lands in a computed "Other" section, so a new command can
# never silently vanish from --help.
_HELP_GROUPS = [
    ("Start here",
     ["run", "chat", "init", "auth", "doctor", "config", "model", "update", "version"]),
    ("Surfaces",
     ["gateway", "telegram", "whatsapp", "email", "discord", "slack", "signal",
      "x", "serve", "dashboard"]),
    ("Autonomy & work",
     ["goals", "cron", "session", "subagents", "skills", "skill", "approvals",
      "surface", "todos"]),
    ("Money",
     ["wallet", "finance"]),
    ("Inspect & admin",
     ["tools", "kb", "knowledge", "owner", "journey", "pfp", "soul",
      "x-account", "datagen"]),
]


class _LazyGroup(click.Group):
    """Click group that imports a subcommand's module only when it is invoked.

    The standard lazy-loading pattern from the click docs: ``list_commands``
    advertises the names, ``get_command`` imports on demand. ``--help`` still
    loads every module (it needs each short help), but a normal invocation
    imports exactly one.
    """

    def __init__(self, *args, lazy_subcommands=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.lazy_subcommands = dict(lazy_subcommands or {})

    def list_commands(self, ctx):
        return sorted(set(super().list_commands(ctx)) | set(self.lazy_subcommands))

    def get_command(self, ctx, cmd_name):
        if cmd_name in self.lazy_subcommands:
            return self._load_lazy(cmd_name)
        return super().get_command(ctx, cmd_name)

    def _load_lazy(self, cmd_name):
        modname, attr = self.lazy_subcommands[cmd_name].split(":", 1)
        cmd = getattr(importlib.import_module(modname), attr)
        if not isinstance(cmd, click.Command):
            raise TypeError(
                f"lazy subcommand {cmd_name!r} resolved to {cmd!r}, not a click.Command"
            )
        return cmd

    def format_commands(self, ctx, formatter):
        """Grouped help (027 WP6): sections instead of 40 flat rows; alias
        names collapse onto their canonical row."""
        available = {}
        for name in self.list_commands(ctx):
            if name in _ALIAS_NAMES:
                continue
            cmd = self.get_command(ctx, name)
            if cmd is None or cmd.hidden:
                continue
            available[name] = cmd
        if not available:
            return

        listed = set()
        sections = list(_HELP_GROUPS)
        leftover = [n for n in sorted(available)
                    if not any(n in names for _, names in sections)]
        if leftover:
            sections.append(("Other", leftover))

        limit = formatter.width - 6 - max(
            (len(n) + (len(f" (alias: {', '.join(_COMMAND_ALIASES[n])})")
                       if n in _COMMAND_ALIASES else 0))
            for n in available)
        for title, names in sections:
            rows = []
            for name in names:
                if name not in available or name in listed:
                    continue
                listed.add(name)
                display = name
                if name in _COMMAND_ALIASES:
                    display += f" (alias: {', '.join(_COMMAND_ALIASES[name])})"
                rows.append((display, available[name].get_short_help_str(limit=limit)))
            if rows:
                with formatter.section(title):
                    formatter.write_dl(rows)


@click.group(cls=_LazyGroup, lazy_subcommands=_LAZY_SUBCOMMANDS, invoke_without_command=True)
@click.version_option(VERSION, "-V", "--version", prog_name="polyrob", message="polyrob v%(version)s")
@click.option("--plain", is_flag=True, help="Force plain, line-oriented output (no ANSI / toolbar)")
@click.option("--project", "project", default=None, metavar="PATH",
              help="Persistent project workspace: agent reads/writes here across sessions "
                   "(sets POLYROB_PROJECT_DIR). Like launching Claude Code in a folder.")
@click.option("--model", "-m", default=None, help="Model for this REPL session (parity with `polyrob run`)")
@click.option("--provider", "-p", default=None, help="Provider for this REPL session")
@click.option("--toolset", default=None, help="Named toolset for this REPL session")
@click.pass_context
def cli(ctx, plain, project, model, provider, toolset):
    """POLYROB AI automation platform CLI."""
    if project:
        import os
        from pathlib import Path
        os.environ["POLYROB_PROJECT_DIR"] = str(Path(project).resolve())
    if ctx.invoked_subcommand is None:
        _start_repl(plain=plain, model=model, provider=provider, toolset=toolset)


def _start_repl(plain, model, provider, toolset):
    """Show instant feedback, then import + run the REPL.

    The transient ``starting…`` notice is written BEFORE the chat-module /
    bootstrap imports, so a cold start shows feedback within milliseconds
    instead of a blank terminal (the "empty loading" complaint). The terminal
    tab title is set here too — without it the tab shows the interpreter's
    process name ("Python") instead of the product + version.
    """
    from cli.ui import terminal_title
    from cli.ui.bootstrap_notice import show_start_notice
    from core.env import bool_env

    stream = sys.stdout
    _plain = plain or bool_env("POLYROB_PLAIN", False)
    _titled = (not _plain) and terminal_title.set_terminal_title(
        f"polyrob {VERSION}", stream
    )
    try:
        transient = show_start_notice(stream)

        from cli.commands.chat import run_repl
        run_repl(plain=plain, model=model, provider=provider, toolset=toolset,
                 start_notice=(stream, transient))
    finally:
        if _titled:
            terminal_title.clear_terminal_title(stream)


@cli.command()
def version():
    """Show POLYROB version and environment info."""
    click.echo(f"polyrob v{VERSION}")
    click.echo(f"python {sys.version.split()[0]}")

    try:
        from core.config import BotConfig
        click.echo("core: available")
    except ImportError:
        click.echo("core: not found (run from project root)")


@cli.command("chat")
@click.option("--plain", is_flag=True, help="Force plain, line-oriented output (no ANSI / toolbar)")
@click.option("--model", "-m", default=None, help="Model for this REPL session (parity with `polyrob run`)")
@click.option("--provider", "-p", default=None, help="Provider for this REPL session")
@click.option("--toolset", default=None, help="Named toolset for this REPL session")
def chat_cmd(plain, model, provider, toolset):
    """Open the interactive REPL chat session."""
    _start_repl(plain=plain, model=model, provider=provider, toolset=toolset)


def main():
    """Entry point for [project.scripts]."""
    cli()


if __name__ == "__main__":
    main()
