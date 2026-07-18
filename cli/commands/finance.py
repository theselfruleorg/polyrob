"""`polyrob finance` — the CLI mirror of the webview /finance balance sheet.

Shares the pure renderer in ``cli/ui/commands/h_finance.py`` (one source of
truth over ``modules.credits.unified_ledger.build_ledger``), resolving the
tenant the same way ``polyrob journey`` does.
"""
from __future__ import annotations

from typing import Optional

import click


@click.command("finance")
@click.option("--days", default=7, type=int, help="Trailing window in days (default 7)")
@click.option("--user", "user", default=None, help="Tenant id (defaults to this instance's owner)")
def finance(days: int, user: Optional[str]) -> None:
    """Balance sheet: income, spend, pending invoices, net — plus runtime cost."""
    import os
    from core.bootstrap import setup_project_path, setup_sqlite_compat, load_env
    from core.identity import resolve_identity
    from cli.ui.commands.h_finance import render_finance

    setup_project_path()
    setup_sqlite_compat()
    # H14a/M16: bootstrap the local env so a file-set POLYROB_OWNER_USER_ID is seen
    # (else the tenant silently resolves to "local" and the sheet reads $0). Mirrors
    # owner.py / journey.
    try:
        load_env(local_mode=True)
    except Exception:
        pass

    uid = (user or resolve_identity() or "").strip() or "local"

    # H14a: resolve the live bot.db so `finance` works standalone (no DI container) —
    # otherwise build_ledger's container lookup raises and the sheet renders the
    # developer-speak "unavailable" line on every run. DB_PATH wins, else the CLI
    # data-home layout.
    def _bot_db_path():
        env_db = os.getenv("DB_PATH")
        if env_db and os.path.isfile(env_db):
            return env_db
        try:
            from core.bootstrap import _resolve_cli_data_home
            from core.db_manifest import candidate_sqlite_dbs
            data_home, _, _ = _resolve_cli_data_home()
            for p in candidate_sqlite_dbs(data_home):
                if p.name == "bot.db" and p.is_file():
                    return str(p)
        except Exception:
            pass
        return None

    # standalone=True: this Click command has no DI container, so if no bot.db is
    # resolved render_finance renders an honest "no data yet" sheet rather than
    # crashing into the container's developer-speak init error (H14a).
    click.echo(render_finance(user_id=uid, days=days, db_path=_bot_db_path(),
                              standalone=True))
