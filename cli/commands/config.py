"""`polyrob config` — show / set / path for file-first config (R7).

Project (./.polyrob/.env) overrides global (~/.polyrob/.env). Secret values are
redacted in `show`. No DB, no TOML — plain dotenv files.
"""
from __future__ import annotations
from pathlib import Path

import click

from core.paths import polyrob_home
from core.secrets import is_secret_key as _is_secret_key


def _is_secret(key: str) -> bool:
    """Delegate to the SSOT display-redaction predicate in core.secrets."""
    return _is_secret_key(key)


def _redact(value: str) -> str:
    if len(value) <= 4:
        return "****"
    return f"{value[:2]}***{value[-2:]}"


def _upsert_env(path: Path, key: str, value: str, secure: bool) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    # Index by the whitespace-stripped key so a hand-edited `KEY = value` line
    # (spaces around `=`) is matched by a later `config set KEY …` instead of
    # appending a duplicate. Case-sensitivity is preserved; only surrounding
    # whitespace is normalized.
    index = {ln.split("=", 1)[0].strip(): i for i, ln in enumerate(lines) if "=" in ln}
    key = key.strip()
    line = f"{key}={value}"
    if key in index:
        lines[index[key]] = line
    else:
        lines.append(line)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    if secure:
        path.chmod(0o600)


def _read_env_file(path: Path) -> dict:
    result: dict = {}
    if path.exists():
        for ln in path.read_text().splitlines():
            stripped = ln.strip()
            if "=" in stripped and not stripped.startswith("#"):
                k, v = stripped.split("=", 1)
                result[k.strip()] = v.strip()
    return result


@click.group("config")
def config():
    """Show or edit POLYROB configuration (file-first: ~/.polyrob + ./.polyrob)."""


@config.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--global", "is_global", is_flag=True, default=False,
              help="Write to ~/.polyrob/.env (default: ./.polyrob/.env)")
def set_cmd(key, value, is_global):
    """Set KEY=VALUE in the project (default) or global (--global) env file."""
    # Both env files may hold API keys, so lock them to 0600 regardless of scope.
    path = (polyrob_home() if is_global else Path.cwd() / ".polyrob") / ".env"
    _upsert_env(path, key, value, secure=True)
    if not is_global:
        # Project scope writes ./.polyrob/.env — which may hold a secret — so make
        # sure .polyrob/ is gitignored BEFORE the user can `git add` it. Previously
        # only `init`/`run` did this; `config set` was a leak path in between.
        from cli.gitignore import ensure_polyrob_gitignored
        ensure_polyrob_gitignored(Path.cwd(), require_git_repo=True)
    click.echo(f"Set {key} in {path}")


@config.command("show")
def show_cmd():
    """Show effective config (project overrides global); secrets redacted."""
    merged: dict = {}
    merged.update(_read_env_file(polyrob_home() / ".env"))
    merged.update(_read_env_file(Path.cwd() / ".polyrob" / ".env"))  # project wins
    if not merged:
        click.echo("(no config set — run `polyrob init` or `polyrob config set KEY VALUE`)")
        return
    for key in sorted(merged):
        value = _redact(merged[key]) if _is_secret(key) else merged[key]
        click.echo(f"{key}={value}")


@config.command("path")
def path_cmd():
    """List the config files that contribute (project first = higher precedence)."""
    for label, path in (("project", Path.cwd() / ".polyrob" / ".env"),
                        ("global", polyrob_home() / ".env")):
        status = "exists" if path.exists() else "absent"
        click.echo(f"[{label}] {path} ({status})")
