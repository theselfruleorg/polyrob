"""POLYROB CLI entry point.

Bare ``polyrob`` opens the chat REPL. ``polyrob --help`` lists every command,
grouped (Start here / Surfaces / Autonomy & work / Money / Owner — 027 WP6,
regrouped 043 A14); first-run path: ``polyrob init`` → ``polyrob doctor`` →
``polyrob``.

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
    "rails": "cli.commands.rails:rails",  # 036: standing work (rails + grants)
    "model": "cli.commands.model:model",
    "models": "cli.commands.model:model",  # product vocabulary alias
    "skills": "cli.commands.skills:skills",
    "skill": "cli.commands.skill_install:skill",
    "tools": "cli.commands.tools:tools",
    "init": "cli.commands.init:init_cmd",
    "setup": "cli.commands.init:init_cmd",  # the word every other agent CLI uses
    "service": "cli.commands.service:service",  # 062: run the agent in the background
    "uninstall": "cli.commands.uninstall:uninstall_cmd",  # 062
    "config": "cli.commands.config:config",
    "profile": "cli.commands.profile:profile",
    "profiles": "cli.commands.profile:profile",  # product vocabulary alias
    "auth": "cli.commands.auth:auth",
    "keys": "cli.commands.keys:keys",  # 043 A30: the owner's API-key seat
    "doctor": "cli.commands.doctor:doctor",
    "owner": "cli.commands.owner:owner",
    "kb": "cli.commands.kb:kb",
    "serve": "cli.commands.serve:serve",
    "dashboard": "cli.commands.dashboard:dashboard",
    "webgate": "cli.commands.dashboard:dashboard",  # alias
    "surface": "cli.commands.surface:surface",
    "surfaces": "cli.commands.surfaces:surfaces",  # 064 F6: list / add / probe from the catalog
    "gateway": "cli.commands.gateway:gateway",
    "goals": "cli.commands.goals:goals",
    "cron": "cli.commands.cron:cron",
    "autonomy": "cli.commands.autonomy:autonomy",  # 030 WS-E4 intent verbs
    "apps": "cli.commands.apps:apps",  # 032 durable app service (owner seat + supervisor)
    "subagents": "cli.commands.subagents:subagents",
    "workers": "cli.commands.workers:workers",  # 041 phase 2: named workers (owner seat)
    "todos": "cli.commands.todos:todos",
    "update": "cli.commands.update:update_cmd",
    "avatar": "cli.commands.avatar:avatar",  # the avatar slot (core/avatar.py)
    "soul": "cli.commands.soul:soul",
    "persona": "cli.commands.persona:persona",  # F4: the character-authoring seat
    "identity": "cli.commands.identity:identity",  # 043 A14: soul/persona/avatar umbrella
    "journey": "cli.commands.journey:journey",
    "finance": "cli.commands.finance:finance",
    "wallet": "cli.commands.wallet:wallet_cmd",
    "datagen": "cli.commands.datagen:datagen",
    "knowledge": "cli.commands.knowledge:knowledge",  # hidden deprecated alias for `kb export` (030 D10)
    "approvals": "cli.commands.approvals:approvals",
    "browser": "cli.commands.browser:browser",  # 049: the isolated browser service (custody)
    "pack": "cli.commands.pack:pack",  # 067 P2: installed packs
}


def _surface_commands() -> dict:
    """``polyrob <surface>`` for every CORE chat surface — from its catalog row
    (064 F1). A pack surface's command is a pack CLI command (067 P3b), served
    through ``_pack_commands`` and the pack's own gating."""
    from core.surfaces.catalog import cli_commands
    return cli_commands(core_only=True)


_LAZY_SUBCOMMANDS.update(_surface_commands())

# --- Help-surface layout (027 WP6, regrouped 043 A14) ---
# canonical name -> alias names. Aliases stay invocable but render on the
# canonical row ("session (alias: sessions)"), never as duplicate entries.
# `soul`/`persona`/`avatar` fold onto `identity` (the new umbrella group, A14);
# `approvals` folds onto `owner` (a display-only alias line for now — `owner
# approvals` as a real subcommand is phase 3 create work, 043 §4.1); `skill`
# (the single-skill install pipeline) folds onto `skills`.
_COMMAND_ALIASES = {
    "session": ("sessions",),
    "model": ("models",),
    "dashboard": ("webgate",),
    "profile": ("profiles",),
    "identity": ("soul", "persona", "avatar"),
    "owner": ("approvals",),
    "skills": ("skill",),
    "setup": ("init",),
}
_RELATED_COMMANDS = {name: _COMMAND_ALIASES.pop(name) for name in ("identity", "owner", "skills")}
_DISPLAY_FOLDS = {**_COMMAND_ALIASES, **_RELATED_COMMANDS}
_ALIAS_NAMES = {alias for aliases in _DISPLAY_FOLDS.values() for alias in aliases}

# Grouped --help: a first-run user needs "start here", not 40 flat rows.
# Anything unlisted lands in a computed "Other" section, so a new command can
# never silently vanish from --help.
_HELP_GROUPS = [
    ("Start here",
     ["run", "chat", "setup", "auth", "keys", "doctor", "config", "model",
      "update", "uninstall", "version"]),
    ("Surfaces",
     ["gateway", "surfaces", *_surface_commands(), "serve", "dashboard", "service"]),
    ("Autonomy & work",
     ["goals", "cron", "session", "subagents", "workers", "skills", "surface", "todos",
      "apps", "autonomy", "tools", "kb", "browser", "rails"]),
    ("Money",
     ["wallet", "finance", "journey"]),
    ("Owner",
     ["owner", "identity", "profile", "pack"]),
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

    def _lazy_map(self) -> dict:
        # A surface catalog row added after import (a test, a plugin) still
        # gets its command: the surface half is re-read on each lookup.
        return {**_surface_commands(), **self.lazy_subcommands}

    @staticmethod
    def _pack_commands() -> dict:
        """067 P2: command name -> pack id, from the installed packs' pack.toml
        (read in ``main()``'s phase 1; nothing is imported). Core names win."""
        from core.packs.state import cli_command_owners
        return cli_command_owners()

    def list_commands(self, ctx):
        return sorted(set(super().list_commands(ctx)) | set(self._lazy_map())
                      | set(self._pack_commands()))

    def get_command(self, ctx, cmd_name):
        if cmd_name in self._lazy_map():
            return self._load_lazy(cmd_name)
        cmd = super().get_command(ctx, cmd_name)
        if cmd is None and cmd_name in self._pack_commands():
            from cli.commands.pack import PackCommand
            cmd = PackCommand(cmd_name, self._pack_commands()[cmd_name])
        return cmd

    def _load_lazy(self, cmd_name):
        modname, attr = self._lazy_map()[cmd_name].split(":", 1)
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
        # 067 P3b: a pack surface's command (X's `polyrob x`) sits with the core
        # surface commands, not in "Other".
        from core.surfaces.catalog import cli_commands as _all_surface_commands
        sections = [(title, names + [n for n in _all_surface_commands() if n not in names])
                    if title == "Surfaces" else (title, names)
                    for title, names in _HELP_GROUPS]
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
                elif name in _RELATED_COMMANDS:
                    display += f" (related: {', '.join(_RELATED_COMMANDS[name])})"
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
@click.option("--profile", "-P", "profile", default=None, metavar="NAME",
              help="Named profile to run as (an isolated home under "
                   "~/.polyrob/profiles/<NAME>: its own .env, characters, "
                   "memory, identity). See `polyrob profile --help`.")
@click.pass_context
def cli(ctx, plain, project, model, provider, toolset, profile):
    """POLYROB AI automation platform CLI."""
    # Profile resolution MUST run before anything else — flags freeze at import
    # and every subcommand's load_env refreezes them once; the profile env
    # (POLYROB_HOME/POLYROB_DATA_DIR) has to be in place first.
    from core.profiles import ProfileError, activate_profile
    try:
        activate_profile(profile)
    except ProfileError as exc:
        raise click.ClickException(str(exc))
    ctx.ensure_object(dict)
    ctx.obj["cli_defaults"] = dict(plain=plain, model=model, provider=provider, toolset=toolset)
    if plain:
        ctx.color = False
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
    from cli.commands._options import inherit_options
    values = inherit_options(plain=plain, model=model, provider=provider, toolset=toolset)
    _start_repl(**values)


def _register_pack_policies() -> None:
    """067 P2 phase 1: pack POLICY rows (data from each ``pack.toml``; no pack
    code runs) register here, before a subcommand imports the policy views.
    Never stops the CLI — a refused pack is named in ``polyrob pack doctor``."""
    try:
        from core.packs.loader import register_policies
        register_policies()
    except Exception as exc:  # noqa: BLE001
        print(f"polyrob: pack discovery failed: {type(exc).__name__}: {exc}", file=sys.stderr)


def _refuse_agent_child_owner_verb() -> None:
    """EXEC-1: an owner-only verb refuses in a process the agent started
    (``cli/agent_child_gate.py``). Read verbs stay available to the agent."""
    from cli.agent_child_gate import refusal
    message = refusal(sys.argv[1:])
    if message:
        print(message, file=sys.stderr)
        sys.exit(1)


def main():
    """Entry point for [project.scripts]."""
    _register_pack_policies()
    _refuse_agent_child_owner_verb()
    cli()


if __name__ == "__main__":
    main()
