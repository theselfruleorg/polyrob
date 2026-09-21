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

057 WS-G — the euid guard
-------------------------
The seam is also where a ROOT-run owner verb is caught. On a deployed box the
shared data home is written by three de-rooted units as members of
``polyrob-data``; a verb typed as root creates root-owned 0644 rows there, and
the SERVICE then meets them as ``attempt to write a readonly database`` hours
later, in a unit that did nothing wrong.

The seam cannot see whether its caller is about to read or to write, so it is
told:

* ``write=True``  — a mutating verb. Root on a DEPLOYED box REFUSES with the
  remedy (``sudo -u polyrob-agent polyrob …``), unless ``--as-root`` or
  ``POLYROB_ALLOW_ROOT_CLI=1``.
* ``write=False`` — a read-only verb (``doctor``). Never refused; root reads
  are harmless and the operator needs them most when something is broken.
* ``write=None``  — the caller has not said. Root on a deployed box gets ONE
  warning per process that names what it cannot tell, and the verb proceeds.
  A warning that admits the gap is honest; a refusal here would break every
  read-only verb that has not been classified yet.

A box with nothing deployed (every dev checkout) is untouched and silent: the
rule is about a SHARED home, and there is no sharing there.
"""
from __future__ import annotations

import os
from typing import Optional

import click

#: Set this (to a truthy value) for a script that genuinely must run a mutating
#: owner verb as root — the deploy's own steps, a rescue shell.
ALLOW_ROOT_ENV = "POLYROB_ALLOW_ROOT_CLI"

#: ``--as-root`` sets this for the life of the process (the option is eager, so
#: it is applied before the verb body resolves its data home).
_as_root_flag = False

#: The `write=None` warning is per-process-once: `polyrob owner pending` calls
#: the seam twice in one command and must not say it twice.
_WARNED: set = set()


def reset_root_guard_state() -> None:
    """Forget ``--as-root`` and the once-per-process warning (tests)."""
    global _as_root_flag
    _as_root_flag = False
    _WARNED.clear()


def _set_as_root(ctx, param, value):  # click eager-option callback
    global _as_root_flag
    if value:
        _as_root_flag = True
    return value


def as_root_option(f):
    """``--as-root``: run this mutating owner verb as root anyway.

    Shared so every verb spells the escape hatch the same way. Eager +
    ``expose_value=False`` so the command body never has to thread it.
    """
    return click.option(
        "--as-root", is_flag=True, expose_value=False, is_eager=True,
        callback=_set_as_root,
        help="Run as root anyway (writes root-owned rows into the shared data "
             "home; prefer `sudo -u polyrob-agent polyrob …`).")(f)


def root_cli_allowed() -> bool:
    if _as_root_flag:
        return True
    # Literal, not ALLOW_ROOT_ENV: the reverse flag-catalog scan reads
    # `bool_env(<literal name>, …)`, and a flag read through a constant is invisible to it.
    from core.env import bool_env
    return bool_env("POLYROB_ALLOW_ROOT_CLI", False)


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid) and geteuid() == 0


def _deployed_box() -> bool:
    """True when this box carries a deployed instance (env file / units).

    The whole rule is about a home SHARED with running services; a dev checkout
    has none, so it is never guarded.
    """
    try:
        from core.admin_data_home import _deployment_evidence
        return bool(_deployment_evidence())
    except Exception:
        return False


def _root_remedy(path: str) -> str:
    return (f"refusing to write {path} as root: the shared data home is written by "
            f"the de-rooted service identities (group polyrob-data), and a root-run "
            f"verb leaves rows they cannot write — the service meets them later as "
            f"'attempt to write a readonly database'.\n"
            f"    sudo -u polyrob-agent polyrob …    (the same verb, the right identity)\n"
            f"Override deliberately with --as-root, or {ALLOW_ROOT_ENV}=1 for a script.")


def check_root_write(path: str, write: "bool | None") -> None:
    """Apply the euid rule to one resolved data home. Raises on a refusal."""
    if write is False or not _is_root() or root_cli_allowed() or not _deployed_box():
        return
    if write:
        raise click.ClickException(_root_remedy(path))
    key = f"warn:{path}"
    if key in _WARNED:
        return
    _WARNED.add(key)
    click.echo(click.style(
        f"warning: running as root against the shared data home {path}. This seam "
        f"cannot tell whether this verb writes; if it does, it leaves rows the "
        f"service identities cannot write. Prefer "
        f"`sudo -u polyrob-agent polyrob …` (silence this with --as-root or "
        f"{ALLOW_ROOT_ENV}=1).", fg="yellow"), err=True)


def admin_data_dir(*, write: "bool | None" = None) -> str:
    """The data home this owner/admin verb acts on.

    *write* declares the caller's intent for the euid guard (see module doc):
    ``True`` = mutating (root refuses on a deployed box), ``False`` = read-only
    (never refused), ``None`` = unknown (root warns once and proceeds).
    """
    from core.admin_data_home import AmbiguousDataHome, admin_data_home
    try:
        path = admin_data_home(
            echo=lambda m: click.echo(click.style(m, fg="yellow"), err=True))
    except AmbiguousDataHome as exc:
        raise click.ClickException(str(exc))
    check_root_write(path, write)
    return path


def admin_bot_db_path() -> Optional[str]:
    """The live ``bot.db`` under the deployed home, or None when there is none.

    ``DB_PATH`` wins (an explicit operator pin), else the db-manifest candidates
    under ``admin_data_dir(write=False)``. Shared by ``polyrob finance`` and
    ``polyrob doctor`` (2026-09-21) so the two money readers cannot open
    different stores; a standalone CLI has no DI container to ask.
    """
    import os
    env_db = os.getenv("DB_PATH")
    if env_db and os.path.isfile(env_db):
        return env_db
    try:
        from core.db_manifest import candidate_sqlite_dbs
        for p in candidate_sqlite_dbs(admin_data_dir(write=False)):
            if p.name == "bot.db" and p.is_file():
                return str(p)
    except Exception:
        return None
    return None
