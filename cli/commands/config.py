"""`polyrob config` — show / set / path / check for file-first config (R7, P1 T8).

Project (./.polyrob/.env) overrides global (~/.polyrob/.env). Secret values are
redacted in `show`. No DB, no TOML — plain dotenv files (env-flags) plus a
per-user ``preferences.toml`` (typed prefs — see ``core.prefs``).

``config set`` routes KEY to one of three stores, first match wins:
  1. secret-shaped KEY (``core.secrets.is_secret_key``) -> env file, written
     exactly as before (no validation, redacted echo).
  2. dotted KEY that is a known preference (``core.prefs.PREF_SCHEMA``) ->
     per-user ``preferences.toml`` (requires ``--user``; guarded prefs also
     require ``--confirm``, same rule as the ``/config`` REPL command in
     ``cli/ui/commands/h_config.py``).
  3. KEY documented in the flags catalog (``core.prefs.catalog_lookup``) ->
     env file, but only after the VALUE is checked against the documented
     default's shape (boolish for ON/OFF, numeric for a backtick number).
     (``DEFAULT_MODEL``/``DEFAULT_PROVIDER``/``CHAT_MODEL``/``CHAT_PROVIDER``
     — real, actively-read legacy env vars — got proper catalog rows in the
     owner-UX P1 T10 sweep, so they route here now; there is no longer a
     separate uncataloged-allowlist step.)
  4. otherwise -> hard-reject with ``click.ClickException`` — with a
     closest-match suggestion (across prefs + catalog names) when one exists,
     a generic "not a documented flag or preference" otherwise — unless
     ``--force``, which writes the raw KEY=VALUE.

``config check`` is the CLI counterpart of the ``/config check`` REPL
subcommand: it validates env-flag files against the catalog
(``core.prefs.check_env_files``) and, with ``--user``, the tenant's
``preferences.toml`` (``core.prefs.find_invalid_preferences``). Never prints a
secret VALUE — only key names.
"""
from __future__ import annotations

import difflib
from pathlib import Path
from typing import Optional

import click

from core.paths import polyrob_home
from core.prefs import (
    PREF_SCHEMA,
    SENSITIVITY_GUARDED,
    catalog_lookup,
    catalog_names,
    check_env_files,
    display_effective,
    find_invalid_preferences,
    preferences_path,
    shape_of_default,
    value_matches_shape,
    write_preference,
)
from core.runtime_paths import resolve_runtime_paths
from core.secrets import is_secret_key as _is_secret_key


def _is_secret(key: str) -> bool:
    """Delegate to the SSOT display-redaction predicate in core.secrets."""
    return _is_secret_key(key)


def _redact(value: str) -> str:
    if len(value) <= 4:
        return "****"
    return f"{value[:2]}***{value[-2:]}"


def _upsert_env(path: Path, key: str, value: str, secure: bool) -> None:
    # Promoted to core/env_file.py (018 P1) so core.config_service can write
    # env flags without importing upward into the cli tier; kept as a thin
    # delegator because /model's dual-write (cli/config_store.py) imports it.
    from core.env_file import upsert_env_var
    upsert_env_var(path, key, value, secure=secure)


def _read_env_file(path: Path) -> dict:
    from core.env_file import read_env_file
    return read_env_file(path)


def _env_path(is_global: bool) -> Path:
    return (polyrob_home() if is_global else Path.cwd() / ".polyrob") / ".env"


def _write_env_flag(key: str, value: str, is_global: bool) -> Path:
    """Upsert KEY=VALUE into the resolved env file; project scope also gitignores.

    Both env files may hold API keys, so lock them to 0600 regardless of scope.
    """
    path = _env_path(is_global)
    _upsert_env(path, key, value, secure=True)
    if not is_global:
        # Project scope writes ./.polyrob/.env — which may hold a secret — so make
        # sure .polyrob/ is gitignored BEFORE the user can `git add` it. Previously
        # only `init`/`run` did this; `config set` was a leak path in between.
        from cli.gitignore import ensure_polyrob_gitignored
        ensure_polyrob_gitignored(Path.cwd(), require_git_repo=True)
    return path



def _closest_match(key: str) -> Optional[str]:
    """Closest-match suggestion across BOTH namespaces (prefs + catalog flags)."""
    names = list(PREF_SCHEMA.keys()) + catalog_names()
    hits = difflib.get_close_matches(key, names, n=1)
    return hits[0] if hits else None


def _default_home_dir() -> str:
    """The real data home used for per-user preference storage.

    Resolved the same way the CLI/local container resolves its data home
    (``POLYROB_DATA_DIR`` if set, else ``cwd/.polyrob`` — see
    ``core.runtime_paths.resolve_runtime_paths``). Overridable via the hidden
    ``--home`` option (test/ops only).
    """
    return str(resolve_runtime_paths(local=True).data_home)


@click.group("config")
def config():
    """Show or edit POLYROB configuration (file-first: ~/.polyrob + ./.polyrob)."""


@config.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--global", "is_global", is_flag=True, default=False,
              help="Write to ~/.polyrob/.env (default: ./.polyrob/.env)")
@click.option("--user", "user_id", default=None,
              help="Tenant user id — required to set a per-user preference "
                   "(dotted key, e.g. style.verbosity)")
@click.option("--confirm", is_flag=True, default=False,
              help="Confirm a guarded preference change (required for guarded keys)")
@click.option("--force", is_flag=True, default=False,
              help="Write KEY even if it doesn't match a known preference/flag")
@click.option("--home", "home_dir_opt", default=None, hidden=True,
              help="Override the preferences data home (test/ops only)")
def set_cmd(key, value, is_global, user_id, confirm, force, home_dir_opt):
    """Set KEY=VALUE — routes to a secret, a per-user preference, or an env flag.

    See the module docstring for the full routing decision tree.
    """
    # 1. Secret-shaped KEY -> env file, exactly as before, no validation.
    if _is_secret_key(key):
        path = _write_env_flag(key, value, is_global)
        click.echo(f"Set {key} in {path}")
        return

    # 2. Dotted KEY that is a known per-user preference -> preferences.toml.
    if "." in key and key in PREF_SCHEMA:
        if not user_id:
            raise click.ClickException(
                f"'{key}' is a per-user preference — pass --user <uid> "
                "(in the REPL, use `/config set` instead)."
            )
        spec = PREF_SCHEMA[key]
        if spec.sensitivity == SENSITIVITY_GUARDED and not confirm:
            raise click.ClickException(
                f"'{key}' is guarded — confirm with --confirm "
                "(same rule as `/config set ... --confirm` in the REPL)."
            )
        home_dir = home_dir_opt or _default_home_dir()
        ok, err = write_preference(home_dir, user_id, key, value)
        if not ok:
            raise click.ClickException(err)
        path = preferences_path(home_dir, user_id)
        click.echo(f"Set {key} in {path} (applies: {spec.applies}).")
        return

    # 3. KEY documented in the flags catalog -> shape-checked env-flag write.
    hit = catalog_lookup(key)
    if hit is not None:
        _group, documented_default = hit
        shape = shape_of_default(documented_default)
        if not value_matches_shape(value, shape):
            raise click.ClickException(
                f"{key} expects a {shape} value (documented default: "
                f"{documented_default}); got {value!r}"
            )
        path = _write_env_flag(key, value, is_global)
        click.echo(f"Set {key} in {path} (takes effect: restart).")
        return

    # 4. Otherwise: hard-reject any unrecognized key unless --force — a
    #    semantically-different name (no character-level close match) is no
    #    safer than a typo.
    hint = _closest_match(key)
    if not force:
        if hint is not None:
            raise click.ClickException(
                f"unknown key: {key} (did you mean {hint}?) — pass --force to write it anyway"
            )
        raise click.ClickException(
            f"unknown key: {key} — not a documented flag or preference; "
            "pass --force to write it anyway"
        )
    path = _write_env_flag(key, value, is_global)
    suffix = f"; did you mean {hint}?" if hint is not None else ""
    click.echo(f"Set {key} in {path} (--force override{suffix}).")


@config.command("show")
@click.option("--user", "user_id", default=None,
              help="Tenant user id for the preferences section (default: resolved identity)")
@click.option("--home", "home_dir_opt", default=None, hidden=True,
              help="Override the preferences data home (test/ops only)")
def show_cmd(user_id, home_dir_opt):
    """Show effective config (project overrides global); secrets redacted."""
    merged: dict = {}
    merged.update(_read_env_file(polyrob_home() / ".env"))
    merged.update(_read_env_file(Path.cwd() / ".polyrob" / ".env"))  # project wins
    if not merged:
        click.echo("(no config set — run `polyrob init` or `polyrob config set KEY VALUE`)")
    else:
        for key in sorted(merged):
            value = _redact(merged[key]) if _is_secret(key) else merged[key]
            click.echo(f"{key}={value}")

    # T10 (exposure parity with the Telegram `/config` listing and the webview
    # preferences page — every PREF_SCHEMA key must be visible on every
    # control-plane surface): typed per-user preferences, effective value +
    # source via the SAME `core.prefs.display_effective` SSOT.
    from core.identity import resolve_identity
    tenant = user_id or resolve_identity()
    home_dir = home_dir_opt or _default_home_dir()
    click.echo("")
    click.echo(f"preferences (tenant {tenant}):")
    for key in sorted(PREF_SCHEMA):
        value, source = display_effective(key, tenant, home_dir)
        click.echo(f"  {key} = {value}   ({source})")


@config.command("path")
def path_cmd():
    """List the config files that contribute (project first = higher precedence)."""
    for label, path in (("project", Path.cwd() / ".polyrob" / ".env"),
                        ("global", polyrob_home() / ".env")):
        status = "exists" if path.exists() else "absent"
        click.echo(f"[{label}] {path} ({status})")


@config.command("check")
@click.option("--user", "user_id", default=None,
              help="Also validate this tenant's preferences.toml")
@click.option("--home", "home_dir_opt", default=None, hidden=True,
              help="Override the preferences data home (test/ops only)")
def check_cmd(user_id, home_dir_opt):
    """Validate env-flag files (and, with --user, preferences.toml) for typos/shape mismatches.

    Mirrors the ``/config check`` REPL subcommand
    (``cli/ui/commands/h_config.py``) — never prints a secret VALUE, only key
    names. Exits 0 whether or not findings are reported (diagnostic, not
    fatal).
    """
    global_env = polyrob_home() / ".env"
    project_env = Path.cwd() / ".polyrob" / ".env"
    findings = list(check_env_files([global_env, project_env]))

    if user_id:
        home_dir = home_dir_opt or _default_home_dir()
        for pref_key, err in find_invalid_preferences(home_dir, user_id):
            findings.append(f"preferences.toml: {pref_key}: {err}")

    if not findings:
        click.echo("config check: no findings.")
        return
    click.echo(f"config check: {len(findings)} finding(s):")
    for finding in findings:
        click.echo(f"  - {finding}")
