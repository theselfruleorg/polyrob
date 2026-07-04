"""POLYROB CLI entry point.

Usage:
    polyrob version              Show version info
    polyrob run <task>           Run a task session locally
    polyrob session list         List recent sessions
    polyrob session tail <id>    Stream a session's feed
    polyrob session cancel <id>  Cancel a running session
    polyrob model list           List available models and API key status
    polyrob model set-default    Interactive model picker (no args); or pass <provider> <model>
    polyrob skills list          List registered skill IDs
    polyrob skills validate [id] Validate one or all skills
"""

import asyncio
import signal
import sys
from typing import Optional

import click

from core.version import get_version

VERSION = get_version()


@click.group(invoke_without_command=True)
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
        from cli.commands.chat import run_repl
        run_repl(plain=plain, model=model, provider=provider, toolset=toolset)


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
    from cli.commands.chat import run_repl

    run_repl(plain=plain, model=model, provider=provider, toolset=toolset)


# --- Register subcommands extracted to cli/commands/ (P3: thin entry) ---
from cli.commands.run import run as _run_cmd  # noqa: E402
from cli.commands.session import session as _session_group  # noqa: E402
from cli.commands.model import model as _model_group  # noqa: E402
from cli.commands.skills import skills as _skills_group, get_skill_manager  # noqa: E402,F401
from cli.commands.skill_install import skill as _skill_group  # noqa: E402
from cli.commands.tools import tools as _tools_group  # noqa: E402
from cli.commands.init import init_cmd  # noqa: E402
from cli.commands.config import config as _config_group  # noqa: E402
from cli.commands.doctor import doctor as _doctor_cmd  # noqa: E402
from cli.commands.telegram import telegram as _telegram_cmd  # noqa: E402
from cli.commands.whatsapp import whatsapp as _whatsapp_cmd  # noqa: E402
from cli.commands.email import email as _email_cmd  # noqa: E402
from cli.commands.owner import owner as _owner_group  # noqa: E402
from cli.commands.kb import kb as _kb_cmd  # noqa: E402
from cli.commands.serve import serve as _serve_cmd  # noqa: E402
from cli.commands.dashboard import dashboard as _dashboard_cmd  # noqa: E402
from cli.commands.surface import surface as _surface_group  # noqa: E402
from cli.commands.gateway import gateway as _gateway_cmd  # noqa: E402
from cli.commands.goals import goals as _goals_group  # noqa: E402
from cli.commands.subagents import subagents as _subagents_group  # noqa: E402
from cli.commands.todos import todos as _todos_group  # noqa: E402
from cli.commands.update import update_cmd as _update_cmd  # noqa: E402
from cli.commands.pfp import pfp as _pfp_group  # noqa: E402

cli.add_command(_run_cmd)
cli.add_command(_session_group)
cli.add_command(_session_group, name="sessions")  # product vocabulary alias
cli.add_command(_model_group)
cli.add_command(_model_group, name="models")  # product vocabulary alias
cli.add_command(_skills_group)
cli.add_command(_skill_group)
cli.add_command(_tools_group)
cli.add_command(init_cmd)
cli.add_command(_config_group)
cli.add_command(_doctor_cmd)
cli.add_command(_telegram_cmd)
cli.add_command(_whatsapp_cmd)
cli.add_command(_email_cmd)
cli.add_command(_owner_group)
cli.add_command(_kb_cmd)
cli.add_command(_serve_cmd)
cli.add_command(_dashboard_cmd)
cli.add_command(_dashboard_cmd, name="webgate")  # alias
cli.add_command(_surface_group)
cli.add_command(_gateway_cmd)
cli.add_command(_goals_group)
cli.add_command(_subagents_group)
cli.add_command(_todos_group)
cli.add_command(_update_cmd)
cli.add_command(_pfp_group)


def main():
    """Entry point for [project.scripts]."""
    cli()


if __name__ == "__main__":
    main()
