"""The data home an owner/admin CLI verb acts on — ONE click-shaped seam.

Every ``polyrob`` verb that reads or writes the DAEMON's stores (goals, cron,
apps, surfaces, correspondents, the pause record) resolves through
:func:`admin_data_dir`, which wraps ``core.admin_data_home.admin_data_home``:
``POLYROB_DATA_DIR`` wins; on a box with a deployed instance and no env in
the shell the DEPLOYED home is adopted (with a one-time note); when the
deployed home cannot be read the verb REFUSES rather than act on a home the
service never reads (031). A purely local box is unchanged and silent.

Before this seam, ``owner``/``autonomy`` applied that rule while ``apps``,
``surface``, ``cron``, ``goals`` and ``journey`` read ``resolve_data_home``
directly — and ``polyrob owner pending`` itself built its goal board from a
THIRD resolver, so on a deployed box the asks came from one home and the
correspondents from another.
"""
from __future__ import annotations

import click


def admin_data_dir() -> str:
    from core.admin_data_home import AmbiguousDataHome, admin_data_home
    try:
        return admin_data_home(
            echo=lambda m: click.echo(click.style(m, fg="yellow"), err=True))
    except AmbiguousDataHome as exc:
        raise click.ClickException(str(exc))
