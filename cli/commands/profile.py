"""polyrob profile — named-profile management (multi-instance W3).

A profile is a fully isolated POLYROB home under ``<base>/profiles/<name>/``
(its own ``.env``, ``characters/``, ``skills/``, and a ``data/`` subtree with
identity docs + sidecar DBs). Selection semantics live in ``core/profiles.py``;
this module only manages the directories.

Non-destructive by design: ``create --from-project`` COPIES out of a legacy
folder (never moves), ``delete`` touches exactly one profile after a
confirmation, and nothing here ever writes outside the profile registry, the
sticky file, and the wrapper-bin dir.
"""
import os
import shutil
from datetime import date
from pathlib import Path

import click

from cli.commands._grouped import GroupedGroup

_WRAPPER_MARKER = "# polyrob-profile-wrapper:"

#: Config files cloned by ``create --from <profile>`` (local duplication of an
#: identity on the SAME machine — export/import handles sharing, W5).
_CLONE_CONFIG_FILES = (".env", "cli.json", "mcp.json", "providers.yaml",
                       "config.yaml", "auth.json")
_CLONE_SUBDIRS = ("characters", "skills")

#: Identity-shaped env keys lifted out of a legacy project's ``.polyrob/.env``
#: by ``create --from-project`` (provider keys etc. stay where they are).
_IDENTITY_ENV_KEYS = ("POLYROB_INSTANCE_ID", "BOT_INSTANCE_ID", "POLYROB_PERSONA",
                      "PERSONALITY_DEFAULT_CHARACTER", "POLYROB_OWNER_USER_ID",
                      "BOT_OWNER_USER_ID")

#: Sidecar DBs copied only under ``--include-data``.
_DATA_DB_GLOBS = ("*.db",)


def _bin_dir() -> Path:
    """Wrapper-script dir (``POLYROB_BIN_DIR`` overrides; tests isolate via it)."""
    override = (os.environ.get("POLYROB_BIN_DIR") or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".local" / "bin"


def _read_env_file(path: Path) -> dict:
    """Delegates to the env-file SSOT (core/env_file.py) — never a local parser."""
    from core.env_file import read_env_file
    try:
        return read_env_file(path)
    except Exception:
        return {}


def _upsert_env_file(path: Path, updates: dict) -> None:
    """Multi-key upsert over the env-file SSOT (comments/unrelated lines kept)."""
    from core.env_file import upsert_env_var
    path.parent.mkdir(parents=True, exist_ok=True)
    for key, val in updates.items():
        if val is not None:
            upsert_env_var(path, key, str(val))


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file() and not p.is_symlink():
                    total += p.stat().st_size
            except OSError:
                continue
    except Exception:
        pass
    return total


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n / 1.0:.1f}{unit}"
        n /= 1024.0
    return f"{n}B"


def _live_db_sidecars(home: Path) -> list:
    """Best-effort liveness check: SQLite ``-wal``/``-shm`` sidecars under
    ``data/`` usually mean a process holds the profile open right now."""
    out = []
    data = home / "data"
    try:
        if data.is_dir():
            out = sorted(str(p.name) for p in data.glob("*.db-wal"))
    except Exception:
        pass
    return out


def _write_wrapper(alias_name: str, profile_name: str) -> Path:
    """Write ``<bin>/<alias>`` -> ``exec polyrob -P <profile> "$@"``.

    Refuses to overwrite a file that is not one of our wrappers (marker check).
    """
    bin_dir = _bin_dir()
    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":  # pragma: no cover - windows
        target = bin_dir / f"{alias_name}.bat"
        content = f"@echo off\nREM {_WRAPPER_MARKER} {profile_name}\npolyrob -P {profile_name} %*\n"
    else:
        target = bin_dir / alias_name
        content = (f"#!/bin/sh\n{_WRAPPER_MARKER} {profile_name}\n"
                   f'exec polyrob -P {profile_name} "$@"\n')
    if target.exists() and _WRAPPER_MARKER not in target.read_text(encoding="utf-8", errors="replace"):
        raise click.ClickException(
            f"{target} exists and is not a polyrob profile wrapper — refusing "
            f"to overwrite it. Pick another alias name.")
    target.write_text(content, encoding="utf-8")
    if os.name != "nt":
        target.chmod(0o755)
    return target


def _find_wrappers(profile_name: str) -> list:
    """All wrapper scripts in the bin dir pointing at *profile_name*."""
    out = []
    bin_dir = _bin_dir()
    try:
        if bin_dir.is_dir():
            for p in bin_dir.iterdir():
                try:
                    if p.is_file() and f"{_WRAPPER_MARKER} {profile_name}\n" in p.read_text(
                            encoding="utf-8", errors="replace"):
                        out.append(p)
                except OSError:
                    continue
    except Exception:
        pass
    return out


def _path_on_path(d: Path) -> bool:
    return str(d) in (os.environ.get("PATH") or "").split(os.pathsep)


def _write_profile_yaml(home: Path, name: str, description: str) -> None:
    import yaml
    meta = {
        "name": name,
        "display_name": name,
        "description": description or "",
        "created": date.today().isoformat(),
    }
    (home / "profile.yaml").write_text(
        yaml.safe_dump(meta, sort_keys=False), encoding="utf-8")


def create_profile(name: str, *, from_profile: str = None, from_project: str = None,
                   include_data: bool = False, description: str = "") -> dict:
    """Pure creator (click-free core; adopt + tests reuse it).

    Returns a summary dict: ``{"home": Path, "copied": [str, ...]}``.
    """
    from core.profiles import (InvalidProfileNameError, ProfileNotFoundError,
                               is_safe_profile_name, profiles_root)
    if not is_safe_profile_name(name):
        raise InvalidProfileNameError(name)
    home = profiles_root() / name
    if home.exists():
        raise click.ClickException(
            f"profile '{name}' already exists at {home} (use `polyrob profile "
            f"show {name}` or pick another name)")

    copied = []
    home.mkdir(parents=True)
    for sub in ("characters", "skills", "data"):
        (home / sub).mkdir()
    _write_profile_yaml(home, name, description)
    # The profile IS the instance: pin the id explicitly so the identity axis
    # never depends on ambient env (the §1.3 latent-leak class).
    (home / ".env").write_text(
        f"# POLYROB profile '{name}' — this identity's secrets + flags.\n"
        f"POLYROB_INSTANCE_ID={name}\n", encoding="utf-8")

    if from_profile:
        src = profiles_root() / from_profile
        if not src.is_dir():
            raise ProfileNotFoundError(from_profile, src)
        for fname in _CLONE_CONFIG_FILES:
            f = src / fname
            if f.is_file():
                shutil.copy2(f, home / fname)
                copied.append(fname)
        for dname in _CLONE_SUBDIRS:
            d = src / dname
            if d.is_dir() and any(d.iterdir()):
                shutil.copytree(d, home / dname, dirs_exist_ok=True)
                copied.append(dname + "/")
        # A clone is a NEW instance — same voice, its own identity axis.
        _upsert_env_file(home / ".env", {"POLYROB_INSTANCE_ID": name})

    if from_project:
        src_data = Path(from_project).expanduser().resolve() / ".polyrob"
        if not src_data.is_dir():
            raise click.ClickException(
                f"no legacy data dir at {src_data} — nothing to migrate")
        if (src_data / "identity").is_dir():
            shutil.copytree(src_data / "identity", home / "data" / "identity",
                            dirs_exist_ok=True)
            copied.append("identity/")
        if (src_data / "characters").is_dir() and any((src_data / "characters").iterdir()):
            shutil.copytree(src_data / "characters", home / "characters",
                            dirs_exist_ok=True)
            copied.append("characters/")
        env_keys = _read_env_file(src_data / ".env")
        lifted = {k: v for k, v in env_keys.items() if k in _IDENTITY_ENV_KEYS}
        # A legacy project that pinned only the BOT_INSTANCE_ID alias must not
        # be shadowed by the profile default's canonical POLYROB_INSTANCE_ID.
        if "BOT_INSTANCE_ID" in lifted and "POLYROB_INSTANCE_ID" not in lifted:
            lifted["POLYROB_INSTANCE_ID"] = lifted["BOT_INSTANCE_ID"]
        if lifted:
            _upsert_env_file(home / ".env", lifted)
            copied.extend(sorted(lifted))
        if include_data:
            for pattern in _DATA_DB_GLOBS:
                for db in sorted(src_data.glob(pattern)):
                    shutil.copy2(db, home / "data" / db.name)
                    copied.append(db.name)
            if (src_data / "sessions").is_dir():
                shutil.copytree(src_data / "sessions", home / "data" / "sessions",
                                dirs_exist_ok=True)
                copied.append("sessions/")

    return {"home": home, "copied": copied}


# D7 (proposal 030): sectioned --help instead of one flat alphabetical wall.
_PROFILE_HELP_SECTIONS = [
    ("Lifecycle",
     ["create", "list", "use", "show", "path", "rename", "delete", "adopt",
      "alias"]),
    ("Distribution", ["export", "import", "install", "update", "info"]),
]


@click.group(cls=GroupedGroup, help_sections=_PROFILE_HELP_SECTIONS)
def profile():
    """Manage named profiles (isolated bot identities). See docs/guide."""
    # 026 P1.2 seam: file-set values (e.g. POLYROB_PROFILES_ROOT written via
    # `polyrob config set`) must be visible to every profile verb.
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()


# Backup/restore (W5) + distribution (W6) live in sibling modules to keep this
# one to directory management; they register onto the same group.
def _register_transfer_commands():
    from cli.commands.profile_transfer import export_cmd, import_cmd
    profile.add_command(export_cmd)
    profile.add_command(import_cmd)
    try:
        from cli.commands.profile_dist import info_cmd, install_cmd, update_cmd
        profile.add_command(install_cmd)
        profile.add_command(update_cmd)
        profile.add_command(info_cmd)
    except ImportError:
        pass


_register_transfer_commands()


def _render_service_unit(name: str) -> str:
    """Per-profile systemd unit. POLYROB_PROFILE + POLYROB_PROFILES_ROOT are
    set explicitly (the strong env tier — activate_profile() resolves them at
    process start); without them a spawned daemon would run in legacy mode and
    write into the DEFAULT home, not the profile (known failure mode)."""
    from core.profiles import profiles_root
    import shutil as _shutil
    exe = _shutil.which("polyrob") or "/opt/polyrob/venv/bin/polyrob"
    root = profiles_root()
    return f"""[Unit]
Description=POLYROB agent — profile {name}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment="PYTHONUNBUFFERED=1"
Environment="POLYROB_PROFILES_ROOT={root}"
Environment="POLYROB_PROFILE={name}"
EnvironmentFile=-{root / name / '.env'}
ExecStart={exe} telegram
Restart=on-failure
RestartSec=10
SyslogIdentifier=polyrob-{name}

[Install]
WantedBy=multi-user.target
"""


@profile.command("create")
@click.argument("name")
@click.option("--from", "from_profile", default=None, metavar="PROFILE",
              help="Clone config/.env/characters/skills from an existing profile.")
@click.option("--from-project", "from_project", default=None, metavar="PATH",
              help="Seed identity (docs, characters, identity env keys) from a "
                   "legacy project folder's .polyrob/. Copies, never moves.")
@click.option("--include-data", is_flag=True, default=False,
              help="With --from-project: also copy the sidecar DBs + sessions.")
@click.option("--description", default="", help="One-line description for `profile list`.")
@click.option("--alias/--no-alias", "make_alias", default=True,
              help="Write a ~/.local/bin/<name> wrapper so `<name> …` runs this profile.")
@click.option("--service", "with_service", is_flag=True, default=False,
              help="Also emit a systemd unit for running this profile as a daemon.")
def create_cmd(name, from_profile, from_project, include_data, description,
               make_alias, with_service):
    """Create a new profile NAME."""
    from core.profiles import ProfileError
    try:
        result = create_profile(name, from_profile=from_profile,
                                from_project=from_project,
                                include_data=include_data, description=description)
    except ProfileError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Created profile '{name}' at {result['home']}")
    if result["copied"]:
        click.echo("Copied: " + ", ".join(result["copied"]))
    if make_alias:
        try:
            target = _write_wrapper(name, name)
            click.echo(f"Alias: {target} (runs `polyrob -P {name}`)")
            if not _path_on_path(target.parent):
                click.echo(f"Note: {target.parent} is not on your PATH.")
        except click.ClickException as exc:
            click.echo(f"Alias skipped: {exc.message}", err=True)
    if with_service:
        unit_text = _render_service_unit(name)
        unit_path = Path(f"/etc/systemd/system/polyrob-{name}.service")
        try:
            unit_path.write_text(unit_text, encoding="utf-8")
            click.echo(f"Service unit written: {unit_path}")
            click.echo(f"Enable with: systemctl enable --now polyrob-{name}")
        except OSError:
            local = result["home"] / f"polyrob-{name}.service"
            local.write_text(unit_text, encoding="utf-8")
            click.echo(f"No permission for {unit_path} — unit written to {local}")
            click.echo(f"Install with: sudo cp {local} {unit_path} && "
                       f"sudo systemctl enable --now polyrob-{name}")
    click.echo(f"Activate with: polyrob -P {name}   (or: polyrob profile use {name})")


@profile.command("list")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def list_cmd(as_json):
    """List profiles (active one marked with *)."""
    import json

    from core.profiles import list_profiles, read_sticky_profile
    infos = list_profiles()
    if as_json:
        active = (os.environ.get("POLYROB_PROFILE") or "").strip() or read_sticky_profile()
        click.echo(json.dumps([
            {"name": info.name, "home": str(info.home),
             "description": info.description, "active": info.name == active,
             "data_size_bytes": _dir_size(info.home / "data")}
            for info in infos], indent=2))
        return
    if not infos:
        click.echo("No profiles yet. Create one with: polyrob profile create <name>")
        return
    active = (os.environ.get("POLYROB_PROFILE") or "").strip() or read_sticky_profile()
    for info in infos:
        marker = "*" if info.name == active else " "
        size = _fmt_size(_dir_size(info.home / "data"))
        desc = info.description or ""
        click.echo(f"{marker} {info.name:<20} {size:>8}  {desc}")


@profile.command("use")
@click.argument("name", required=False)
@click.option("--clear", is_flag=True, default=False,
              help="Clear the sticky selection (back to legacy/project mode).")
def use_cmd(name, clear):
    """Set NAME as the sticky active profile for future runs."""
    from core.profiles import (ProfileError, active_profile_file, profile_dir)
    sticky = active_profile_file()
    if clear:
        if sticky.exists():
            sticky.unlink()
        click.echo("Sticky profile cleared (legacy/project mode).")
        return
    if not name:
        raise click.ClickException("pass a profile name, or --clear")
    try:
        home = profile_dir(name)
    except ProfileError as exc:
        raise click.ClickException(str(exc))
    if not home.is_dir():
        raise click.ClickException(
            f"profile '{name}' does not exist. Create it with: polyrob profile create {name}")
    sticky.parent.mkdir(parents=True, exist_ok=True)
    sticky.write_text(name + "\n", encoding="utf-8")
    click.echo(f"Active profile: {name} (sticky — every future run uses it "
               f"unless -P/POLYROB_PROFILE overrides)")


@profile.command("show")
@click.argument("name", required=False)
def show_cmd(name):
    """Show one profile (default: the active selection)."""
    from core.profiles import (ProfileError, profile_dir, resolve_active_profile)
    source = None
    if name:
        try:
            home = profile_dir(name)
        except ProfileError as exc:
            raise click.ClickException(str(exc))
        if not home.is_dir():
            raise click.ClickException(f"profile '{name}' does not exist")
    else:
        sel = resolve_active_profile()
        if sel is None:
            click.echo("No active profile (legacy/project mode).")
            click.echo(f"  config home : {os.environ.get('POLYROB_HOME') or '~/.polyrob'}")
            click.echo(f"  data home   : {os.environ.get('POLYROB_DATA_DIR') or './.polyrob'}")
            return
        name, home, source = sel.name, sel.home, sel.source
    env = _read_env_file(home / ".env")
    chars = sorted(p.stem.replace(".character", "")
                   for p in (home / "characters").glob("*.character.json")) \
        if (home / "characters").is_dir() else []
    sessions_dir = home / "data" / "sessions"
    n_sessions = sum(1 for _ in sessions_dir.iterdir()) if sessions_dir.is_dir() else 0
    click.echo(f"Profile   : {name}" + (f"   (selected via {source})" if source else ""))
    click.echo(f"Home      : {home}")
    click.echo(f"Data      : {home / 'data'}  ({_fmt_size(_dir_size(home / 'data'))})")
    click.echo(f"Instance  : {env.get('POLYROB_INSTANCE_ID') or name}")
    click.echo(f"Character : {env.get('PERSONALITY_DEFAULT_CHARACTER') or (chars[0] if chars else 'polyrob (neutral)')}")
    if chars:
        click.echo(f"Characters: {', '.join(chars)}")
    click.echo(f"Sessions  : {n_sessions}")


@profile.command("path")
@click.argument("name", required=False)
def path_cmd(name):
    """Print the profile's home dir (scriptable)."""
    from core.profiles import (ProfileError, profile_dir, resolve_active_profile)
    if name:
        try:
            home = profile_dir(name)
        except ProfileError as exc:
            raise click.ClickException(str(exc))
        if not home.is_dir():
            raise click.ClickException(f"profile '{name}' does not exist")
    else:
        sel = resolve_active_profile()
        if sel is None:
            raise click.ClickException("no active profile")
        home = sel.home
    click.echo(str(home))


@profile.command("rename")
@click.argument("old")
@click.argument("new")
def rename_cmd(old, new):
    """Rename a profile (updates the sticky file and wrapper scripts)."""
    from core.profiles import (ProfileError, active_profile_file, profile_dir)
    try:
        old_home = profile_dir(old)
        new_home = profile_dir(new)
    except ProfileError as exc:
        raise click.ClickException(str(exc))
    if not old_home.is_dir():
        raise click.ClickException(f"profile '{old}' does not exist")
    if new_home.exists():
        raise click.ClickException(f"profile '{new}' already exists")
    live = _live_db_sidecars(old_home)
    if live:
        raise click.ClickException(
            f"profile '{old}' looks in use ({', '.join(live)}) — stop its "
            f"process first")
    old_home.rename(new_home)
    _upsert_env_file(new_home / ".env", {"POLYROB_INSTANCE_ID": new})
    sticky = active_profile_file()
    if sticky.is_file() and sticky.read_text(encoding="utf-8").strip() == old:
        sticky.write_text(new + "\n", encoding="utf-8")
    for wrapper in _find_wrappers(old):
        wrapper.unlink()
        _write_wrapper(new if wrapper.name == old else wrapper.name, new)
    click.echo(f"Renamed profile '{old}' -> '{new}'")
    click.echo(f"Note: the instance id was updated to '{new}'; identity docs under "
               f"data/identity/ keep their original instance key.")


@profile.command("delete")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip the confirmation prompt.")
@click.option("--force", is_flag=True, default=False,
              help="Delete even when the profile looks in use.")
def delete_cmd(name, yes, force):
    """Delete a profile and ALL its data (memories, identity, sessions)."""
    from core.profiles import (ProfileError, active_profile_file, profile_dir)
    try:
        home = profile_dir(name)
    except ProfileError as exc:
        raise click.ClickException(str(exc))
    if not home.is_dir():
        raise click.ClickException(f"profile '{name}' does not exist")
    live = _live_db_sidecars(home)
    if live and not force:
        raise click.ClickException(
            f"profile '{name}' looks in use ({', '.join(live)}) — stop its "
            f"process first, or pass --force")
    if not yes:
        click.confirm(
            f"Delete profile '{name}' at {home} — its memory, identity docs and "
            f"sessions are gone for good. Continue?", abort=True)
    shutil.rmtree(home)
    sticky = active_profile_file()
    if sticky.is_file() and sticky.read_text(encoding="utf-8").strip() == name:
        sticky.unlink()
    for wrapper in _find_wrappers(name):
        wrapper.unlink()
    click.echo(f"Deleted profile '{name}'.")


@profile.command("adopt")
@click.argument("name")
@click.option("--include-data", is_flag=True, default=False,
              help="Also copy this folder's sidecar DBs + sessions into the profile.")
@click.option("--description", default="", help="One-line description.")
def adopt_cmd(name, include_data, description):
    """Create profile NAME from THIS folder's config and pin the folder to it.

    Copies identity out of ./.polyrob (never moves); future runs from this
    folder select the profile via the ./.polyrob/profile pin.
    """
    from core.profiles import ProfileError
    cwd = Path.cwd()
    try:
        result = create_profile(
            name,
            from_project=str(cwd) if (cwd / ".polyrob").is_dir() else None,
            include_data=include_data, description=description)
    except ProfileError as exc:
        raise click.ClickException(str(exc))
    pin = cwd / ".polyrob" / "profile"
    pin.parent.mkdir(parents=True, exist_ok=True)
    pin.write_text(name + "\n", encoding="utf-8")
    click.echo(f"Created profile '{name}' at {result['home']}")
    if result["copied"]:
        click.echo("Copied: " + ", ".join(result["copied"]))
    click.echo(f"Pinned this folder to it ({pin}). Runs from here now use '{name}'.")


@profile.command("alias")
@click.argument("name")
@click.option("--as", "alias_name", default=None, metavar="ALIAS",
              help="Alias command name (default: the profile name).")
def alias_cmd(name, alias_name):
    """Write a wrapper command so `<alias> ...` runs `polyrob -P NAME ...`."""
    from core.profiles import ProfileError, profile_dir
    try:
        home = profile_dir(name)
    except ProfileError as exc:
        raise click.ClickException(str(exc))
    if not home.is_dir():
        raise click.ClickException(f"profile '{name}' does not exist")
    target = _write_wrapper(alias_name or name, name)
    click.echo(f"Wrote {target}")
    if not _path_on_path(target.parent):
        click.echo(f"Note: {target.parent} is not on your PATH — add it to use "
                   f"`{(alias_name or name)}` directly.")
