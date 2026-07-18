"""`polyrob journey` — the CLI mirror of the REPL /journey timeline.

Shares the pure renderer in ``cli/ui/commands/h_journey.py`` (one source of
truth for did/learned/income/changed), resolving the local owner tenant + data
home the same way the goals CLI does.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import click


@click.command("journey")
@click.option("--since", default="7d", help="Trailing window, e.g. 24h | 7d | 30d (default 7d)")
@click.option("--user", "user", default=None, help="Tenant id (defaults to this instance's owner)")
def journey(since: str, user: Optional[str]) -> None:
    """Timeline: what I did, learned, changed — and my income."""
    from core.bootstrap import setup_project_path, setup_sqlite_compat
    from core.runtime_config import get_data_root
    from core.identity import resolve_identity
    from cli.ui.commands.h_journey import render_journey

    setup_project_path()
    setup_sqlite_compat()

    uid = (user or resolve_identity() or "").strip() or "local"
    data_dir = str(Path(get_data_root()))
    click.echo(render_journey(user_id=uid, since_label=since, data_dir=data_dir))
