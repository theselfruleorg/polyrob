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

    # C26: `resolve_identity()` reads the SHELL's environment, so on a deployed
    # box (systemd exports the owner binding, an SSH shell carries none) the
    # sheet resolved tenant "local" and rendered a confident $0.00 over the
    # agent's real books. The ONE admin resolver adopts the deployment's.
    if user:
        uid = user.strip() or "local"
    else:
        from core.admin_data_home import AmbiguousDataHome, admin_owner_principal
        try:
            uid = (admin_owner_principal() or "").strip() or "local"
        except AmbiguousDataHome as exc:
            raise click.ClickException(str(exc))

    # H14a: resolve the live bot.db so `finance` works standalone (no DI container) —
    # otherwise build_ledger's container lookup raises and the sheet renders the
    # developer-speak "unavailable" line on every run. DB_PATH wins, else the CLI
    # data-home layout.
    # C26 / 2026-09-21: ONE resolver shared with `polyrob doctor`.
    from cli._admin_home import admin_bot_db_path as _bot_db_path

    # standalone=True: this Click command has no DI container, so if no bot.db is
    # resolved render_finance renders an honest "no data yet" sheet rather than
    # crashing into the container's developer-speak init error (H14a).
    click.echo(render_finance(user_id=uid, days=days, db_path=_bot_db_path(),
                              standalone=True))
