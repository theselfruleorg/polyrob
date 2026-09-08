"""`polyrob config` — show / set / path / check for file-first config (R7, P1 T8).

Project (./.polyrob/.env) overrides global (~/.polyrob/.env). Secret values are
redacted in `show`. No DB, no TOML — plain dotenv files (env-flags) plus a
per-user ``preferences.toml`` (typed prefs — see ``core.prefs``).

VALUE is optional: omit it and ``config set`` prompts (hidden for a
secret-shaped KEY, and reading one line from a pipe when stdin is not a tty),
so a credential never lands in shell history or ``ps`` output.

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
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()


def _prompt_for_value(key: str) -> str:
    """Read VALUE interactively when it was omitted from the command line.

    `polyrob config set OPENAI_API_KEY sk-...` puts a live credential in shell
    history (and in `ps` output while it runs). Omitting VALUE now reads it
    instead: hidden for a secret-shaped key, echoed for a plain flag.

    A piped/redirected stdin is honored too (`… | polyrob config set KEY`), so
    scripts and password managers work without ever passing the value as argv.
    Refuses an empty value — writing a blank credential reads as "configured"
    at every gate that only checks presence.
    """
    import sys

    secret = _is_secret_key(key)
    if not sys.stdin.isatty():
        # Non-interactive: consume exactly one line. click.prompt would call
        # getpass here, which cannot read a pipe.
        value = (sys.stdin.readline() or "").strip()
    else:
        value = click.prompt(
            f"{key}", hide_input=secret, default="", show_default=False
        ).strip()
    if not value:
        raise click.ClickException(
            f"no value given for {key} — nothing written "
            "(a blank value would read as 'configured' everywhere)."
        )
    return value


@config.command("set")
@click.argument("key")
@click.argument("value", required=False)
@click.option("--global", "is_global", is_flag=True, default=False,
              help="Write to ~/.polyrob/.env (default for flags: ./.polyrob/.env; "
                   "secrets already default to global)")
@click.option("--project", "project_scope", is_flag=True, default=False,
              help="Write a secret to the per-directory ./.polyrob/.env instead "
                   "of the global file")
@click.option("--user", "user_id", default=None,
              help="Tenant user id — required to set a per-user preference "
                   "(dotted key, e.g. style.verbosity)")
@click.option("--confirm", is_flag=True, default=False,
              help="Confirm a guarded preference change (required for guarded keys)")
@click.option("--force", is_flag=True, default=False,
              help="Write KEY even if it doesn't match a known preference/flag")
@click.option("--home", "home_dir_opt", default=None, hidden=True,
              help="Override the preferences data home (test/ops only)")
def set_cmd(key, value, is_global, project_scope, user_id, confirm, force, home_dir_opt):
    """Set KEY=VALUE — routes to a secret, a per-user preference, or an env flag.

    Omit VALUE to be prompted for it (hidden for a secret-shaped KEY), which
    keeps credentials out of shell history. See the module docstring for the
    full routing decision tree.
    """
    if value is None:
        value = _prompt_for_value(key)

    # 1. Secret-shaped KEY -> env file. 027 WP4: credentials default to the
    #    GLOBAL ~/.polyrob/.env — a project-scope key silently vanishes the
    #    moment the user cd's away (the classic "polyrob stopped seeing my
    #    key"). --project opts back into the per-directory file.
    if _is_secret_key(key):
        secret_global = not project_scope
        path = _write_env_flag(key, value, secret_global)
        click.echo(f"Set {key} in {path}")
        if secret_global and not is_global:
            click.echo(click.style(
                "credentials write to the global ~/.polyrob/.env by default; "
                "pass --project for a per-directory key", dim=True))
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
        # 026 P1.4: enum-shaped flags name their valid set on a typo instead of
        # writing a value the resolver silently degrades.
        from core.config_policy.flag_enums import enum_error
        enum_err = enum_error(key, value)
        if enum_err:
            raise click.ClickException(enum_err)
        shape = shape_of_default(documented_default)
        if not value_matches_shape(value, shape):
            raise click.ClickException(
                f"{key} expects a {shape} value (documented default: "
                f"{documented_default}); got {value!r}"
            )
        path = _write_env_flag(key, value, is_global)
        click.echo(f"Set {key} in {path} (takes effect: restart).")
        # 026 P0.6/P1.6: shadow + clamp honesty from the ONE note builder.
        from core.config_service import post_write_notes
        for note in post_write_notes(key, value, "global" if is_global else "project"):
            click.echo(click.style(note, fg="yellow"))
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


@config.command("unset")
@click.argument("key")
@click.option("--global", "is_global", is_flag=True, default=False,
              help="Remove from ~/.polyrob/.env (default: ./.polyrob/.env)")
def unset_cmd(key, is_global):
    """Remove KEY from the env file — the counterpart of `config set`.

    This is how a stale or malformed credential/flag is cleared without
    hand-editing the file (`polyrob doctor` names this verb on a
    "present but unusable" line). Scoping mirrors `set`: project file by
    default, ~/.polyrob/.env with --global.
    """
    from core.env_file import remove_env_var

    path = _env_path(is_global)
    if remove_env_var(path, key):
        click.echo(f"Removed {key} from {path}")
        return
    other = _env_path(not is_global)
    if key.strip() in _read_env_file(other):
        remedy = (f"polyrob config unset {key} --global" if not is_global
                  else f"polyrob config unset {key}")
        raise click.ClickException(
            f"{key} is not set in {path} — it is set in {other}; run `{remedy}`"
        )
    raise click.ClickException(f"{key} is not set in {path}")


# --- `config migrate` — the explicit replacement for the retired backfill -----
# (W1.1, HANDOFF-env-system-and-key-subscriptions-2026-08-14). The automatic
# env-key backfill (core/bootstrap._backfill_provider_keys) silently imported
# dead production keys and vanished them again the moment ONE real key was set.
# This verb makes the same move explicit, once, with the user choosing per key.

#: Credential-shaped name SUFFIXES for migration. Deliberately suffix-based, not
#: the broad substring hints in ``core.secrets`` — those match flags like
#: MODEL_MAX_TOKENS ("TOKEN") whose migration would violate the backfill's
#: "secrets only, never flags" invariant. Suffix + the flag-value filter below
#: keep the candidate list to actual credentials.
_MIGRATE_SECRET_SUFFIXES = (
    "_API_KEY", "_KEY", "_KEY_ID", "_TOKEN", "_SECRET", "_PASSWORD",
    "_PASSPHRASE", "_JWT", "_SEED", "_MNEMONIC", "_CREDENTIAL",
)


def _looks_like_flag_value(value: str) -> bool:
    """True for boolean/numeric values — a flag wearing a secret-shaped name
    (REQUIRE_DEN_TOKEN=false, MODEL_MAX_TOKENS=4096) is never a credential."""
    s = str(value).strip().strip('"').strip("'").lower()
    if s in ("true", "false", "yes", "no", "on", "off", ""):
        return True
    try:
        float(s)
        return True
    except ValueError:
        return False


def _is_migratable_secret(name: str, value: str) -> bool:
    return name.upper().endswith(_MIGRATE_SECRET_SUFFIXES) \
        and not _looks_like_flag_value(value)


def _migrate_sources() -> list:
    """Legacy secret sources in load_env PRECEDENCE order (highest first): root
    .env beats config/.env.production beats config/.env.development, so a key
    defined in two files migrates with its EFFECTIVE value.

    027 rider: also look under the CODE ROOT, not just the CWD — a pip/checkout
    user running `polyrob config migrate` from any directory used to always see
    "nothing to migrate" because the paths were CWD-relative.
    """
    candidates = [Path(".env"),
                  Path("config") / ".env.production",
                  Path("config") / ".env.development"]
    try:
        from core.runtime_paths import resolve_runtime_paths
        code_root = resolve_runtime_paths(local=True).code_root
        if code_root and Path.cwd().resolve() != Path(code_root).resolve():
            candidates += [Path(code_root) / ".env",
                           Path(code_root) / "config" / ".env.production",
                           Path(code_root) / "config" / ".env.development"]
    except Exception:
        pass
    return candidates


def collect_migration_candidates(home_env: dict) -> list:
    """``(key, value, source_path)`` for each secret-shaped key in a legacy env
    file that is absent from ~/.polyrob/.env. First source wins per key."""
    from dotenv import dotenv_values  # parse parity with core.bootstrap.load_env
    out, seen = [], set(home_env)
    for src in _migrate_sources():
        if not src.exists():
            continue
        for key, value in dotenv_values(str(src)).items():
            if not value or key in seen:
                continue
            if _is_migratable_secret(key, value):
                out.append((key, value, src))
                seen.add(key)
    return out


@config.command("migrate")
@click.option("--all", "take_all", is_flag=True, default=False,
              help="Copy every listed key without per-key confirmation (for scripts).")
def migrate_cmd(take_all):
    """Copy secret keys from the legacy env files into ~/.polyrob/.env.

    Scans root .env and config/.env.{production,development} for
    credential-shaped keys that ~/.polyrob/.env does not have yet, lists them
    BY NAME (values are never shown), and copies the ones you confirm.
    Idempotent: a key already present in ~/.polyrob/.env is never touched.
    This replaces the retired automatic env-key backfill
    (POLYROB_ENV_KEY_BACKFILL).
    """
    home_env_path = polyrob_home() / ".env"
    candidates = collect_migration_candidates(_read_env_file(home_env_path))
    if not candidates:
        click.echo(f"nothing to migrate — no legacy secret keys found that "
                   f"{home_env_path} does not already have")
        return
    click.echo(f"found {len(candidates)} key(s) in legacy env files "
               "(values are never shown):")
    for key, _value, src in candidates:
        click.echo(f"  {key}  ({src})")
    copied = 0
    try:
        for key, value, _src in candidates:
            if take_all or click.confirm(f"copy {key}?", default=True):
                _upsert_env(home_env_path, key, value, secure=True)
                copied += 1
    except click.exceptions.Abort:
        raise click.ClickException(
            "aborted — pass --all to copy every listed key without prompting")
    if copied:
        click.echo(f"copied {copied} key(s) to {home_env_path}")
    else:
        click.echo("no keys copied")


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


#: What each env-file tier is FOR — shown by `config path`. The two .polyrob
#: files are the managed ones (`config set`/`unset`/`auth add` write them);
#: everything below is the legacy server-mode tier: a non-systemd server deploy
#: reads config/.env.{env} (systemd prod uses /etc/polyrob/polyrob.env via the
#: unit file instead), and the CLI only keeps them as the LOWEST layers for
#: back-compat. CLI keys belong in ~/.polyrob/.env — `polyrob config migrate`.
_TIER_NOTES = {
    "project": "managed by `polyrob config set`",
    "home": "managed by `polyrob config set --global`",
    "legacy-home": "legacy home (read-only transition fallback)",
    "root": "legacy server-mode tier (lowest precedence)",
    "config-env": ("legacy server-mode tier (non-systemd server deploys only); "
                   "CLI keys belong in ~/.polyrob/.env — `polyrob config migrate`"),
    "config-env-local": "legacy server-mode tier (local override)",
}


@config.command("path")
def path_cmd():
    """List the env files that configure this process (highest precedence first).

    Derived from the ONE candidate/precedence SSOT
    (``core.paths.env_file_candidates``). The two .polyrob files are always
    shown; a legacy tier (root .env, config/.env.*) is listed only when the
    file actually exists. A relic config/.env.* file the RESOLVED env does not
    read is called out too — the classic "why is my key ignored" trap.
    """
    import os

    from core.paths import env_file_candidates

    resolved = os.environ.get("CONFIG_ENV") or os.environ.get("ENV") or "development"
    display = {"home": "global"}
    click.echo("(process env wins over every file)")
    listed = set()
    for cand in env_file_candidates(resolved, local_mode=True):
        exists = cand.path.exists()
        listed.add(str(cand.path))
        if cand.tier not in ("project", "home") and not exists:
            continue  # absent legacy tiers are noise
        label = display.get(cand.tier, cand.tier)
        note = _TIER_NOTES.get(cand.tier, "")
        click.echo(f"[{label}] {cand.path} ({'exists' if exists else 'absent'})"
                   + (f" — {note}" if note else ""))
    for relic in (Path("config") / ".env.production",
                  Path("config") / ".env.development"):
        if str(relic) not in listed and relic.exists():
            click.echo(
                f"note: {relic} exists but is NOT read (resolved env: {resolved}) — "
                "legacy server file; `polyrob config migrate` copies its keys "
                "to ~/.polyrob/.env")


# --- read verbs over the ONE config control plane (030 WS-F3 / 026 C4) -------
# `polyrob config` had write verbs (set/unset) and file views (show/path) but
# none of the REPL's read verbs — get/list/search/explain existed only inside
# a live chat session (cli/ui/commands/h_config.py). These are the CLI-seat
# parity verbs: read-only, no LLM-key preflight (the group callback only loads
# the env ladder), all over core.config_service so the masking/provenance
# semantics are the service's, never a re-derivation.

def _resolved_tenant(user_id: Optional[str]) -> str:
    from core.identity import resolve_identity
    return user_id or resolve_identity()


def _info_payload(info, *, include_chain: bool = False) -> dict:
    """JSON-safe dict for one ``core.config_service.SettingInfo`` (display-safe
    values only — the service masks secrets before they reach here)."""
    payload = {
        "key": info.key,
        "namespace": info.namespace,
        "kind": info.kind,
        "group": info.group,
        "description": info.description,
        "value": info.effective,
        "source": info.source,
        "applies": info.applies,
        "sensitivity": info.sensitivity,
        "enforcement": info.enforcement,
        "secret": info.secret,
    }
    if include_chain:
        payload["chain"] = [{"value": s.value, "origin": s.origin}
                            for s in info.chain]
    return payload


def _echo_json(obj) -> None:
    import json
    click.echo(json.dumps(obj, indent=2, default=str))


def _unknown_key_error(key: str) -> "click.ClickException":
    hint = _closest_match(key)
    suffix = f" (did you mean {hint}?)" if hint else ""
    return click.ClickException(f"unknown key: {key}{suffix}")


def _is_changed(info) -> bool:
    """True when the effective value comes from somewhere other than a default
    (env var, env file, or a written preference)."""
    src = str(info.source or "")
    return not (src == "default" or src.startswith("default(")
                or src.startswith("built-in"))


@config.command("get")
@click.argument("key")
@click.option("--user", "user_id", default=None,
              help="Tenant user id for preference keys (default: resolved identity)")
@click.option("--home", "home_dir_opt", default=None, hidden=True,
              help="Override the preferences data home (test/ops only)")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Machine-readable output")
def get_cmd(key, user_id, home_dir_opt, as_json):
    """Effective value + source + description for one key (secrets masked)."""
    from core import config_service
    tenant = _resolved_tenant(user_id)
    home_dir = home_dir_opt or _default_home_dir()
    try:
        info = config_service.describe(key, user_id=tenant, home_dir=home_dir)
    except KeyError:
        raise _unknown_key_error(key)
    if as_json:
        _echo_json(_info_payload(info))
        return
    click.echo(f"{info.key} = {info.effective}   ({info.source})")
    click.echo(f"namespace: {info.namespace} | kind: {info.kind} | "
               f"applies: {info.applies}")
    if info.enforcement == "advisory":
        click.echo("enforcement: advisory — steers the agent's prompt only")
    if info.description:
        click.echo(info.description)


@config.command("list")
@click.option("--group", "group_filter", default=None,
              help="Only settings in this group (pref group or flag group)")
@click.option("--changed", is_flag=True, default=False,
              help="Only settings whose value differs from the default")
@click.option("--user", "user_id", default=None,
              help="Tenant user id for preference keys (default: resolved identity)")
@click.option("--home", "home_dir_opt", default=None, hidden=True,
              help="Override the preferences data home (test/ops only)")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Machine-readable output")
def list_cmd(group_filter, changed, user_id, home_dir_opt, as_json):
    """Compact listing of every setting (prefs first, then env flags)."""
    from core import config_service
    tenant = _resolved_tenant(user_id)
    home_dir = home_dir_opt or _default_home_dir()
    infos = []
    for key in config_service.known_keys():
        try:
            info = config_service.describe(key, user_id=tenant, home_dir=home_dir)
        except Exception:
            continue
        if group_filter and info.group != group_filter:
            continue
        if changed and not _is_changed(info):
            continue
        infos.append(info)
    if as_json:
        _echo_json([_info_payload(i) for i in infos])
        return
    if not infos:
        click.echo("no matching settings")
        return
    current_ns = None
    for info in infos:
        if info.namespace != current_ns:
            current_ns = info.namespace
            label = "preferences" if current_ns == "pref" else "env flags"
            click.echo(click.style(f"[{label}]", bold=True))
        click.echo(f"  {info.key} = {info.effective}   "
                   f"({info.source}, group: {info.group})")


@config.command("search")
@click.argument("text", nargs=-1, required=True)
@click.option("--user", "user_id", default=None,
              help="Tenant user id for preference keys (default: resolved identity)")
@click.option("--home", "home_dir_opt", default=None, hidden=True,
              help="Override the preferences data home (test/ops only)")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Machine-readable output")
def search_cmd(text, user_id, home_dir_opt, as_json):
    """Fuzzy name+description search across prefs and the ~490 env flags."""
    from core import config_service
    tenant = _resolved_tenant(user_id)
    home_dir = home_dir_opt or _default_home_dir()
    query = " ".join(text)
    hits = config_service.search(query, user_id=tenant, home_dir=home_dir,
                                 limit=25)
    if as_json:
        _echo_json([_info_payload(i) for i in hits])
        return
    if not hits:
        click.echo(f"no settings matching {query!r}")
        return
    for info in hits:
        click.echo(f"{info.key} = {info.effective}   "
                   f"({info.source}, applies: {info.applies})")


@config.command("explain")
@click.argument("key")
@click.option("--user", "user_id", default=None,
              help="Tenant user id for preference keys (default: resolved identity)")
@click.option("--home", "home_dir_opt", default=None, hidden=True,
              help="Override the preferences data home (test/ops only)")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Machine-readable output")
def explain_cmd(key, user_id, home_dir_opt, as_json):
    """Full provenance chain for one key (`git config --show-origin` style)."""
    from core import config_service
    tenant = _resolved_tenant(user_id)
    home_dir = home_dir_opt or _default_home_dir()
    try:
        info = config_service.explain(key, user_id=tenant, home_dir=home_dir)
    except KeyError:
        raise _unknown_key_error(key)
    if as_json:
        _echo_json(_info_payload(info, include_chain=True))
        return
    click.echo(f"{info.key} = {info.effective}   ({info.source})")
    click.echo(f"namespace: {info.namespace} | kind: {info.kind} | "
               f"applies: {info.applies}")
    if info.enforcement == "advisory":
        click.echo("enforcement: advisory — steers the agent's prompt only")
    if info.description:
        click.echo(info.description)
    if info.chain:
        click.echo(click.style("provenance (highest wins):", bold=True))
        for rung in info.chain:
            click.echo(f"  {rung.origin}: {rung.value}")


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
