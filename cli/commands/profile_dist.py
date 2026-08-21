"""polyrob profile install / update / info — profile distribution (W6).

The SHAREABLE personality format (export/import in ``profile_transfer.py`` is
backup, not distribution). A distribution is a git repo (or local dir) with a
``polyrob.profile.yaml`` manifest at its root:

    name: scout
    version: "1.0"
    description: A research-first bot
    author: someone
    license: MIT
    polyrob_requires: ">=0.11"
    env_requires:
      - name: ANTHROPIC_API_KEY
        description: LLM provider key
        required: true
    distribution_owned:      # optional EXTRA owned paths (validated)
      - prompts/

Ownership contract (the part users rely on):

- distribution-owned, REPLACED on update: ``polyrob.profile.yaml``,
  ``characters/``, ``skills/``, ``cron/``, ``mcp.json``, and a shipped
  ``soul.md`` (installed to ``data/identity/<instance>/soul.md`` — SOUL is
  operator-authored/frozen, exactly what a distribution ships);
- distribution-owned but PRESERVED on update unless ``--force-config``:
  ``config.yaml``;
- user-owned, NEVER touched: ``.env``, ``auth.json``, wallet material, and the
  whole ``data/`` tree (memory.db, goals.db, sessions, the agent-written
  ``self.md``/``owner.md``). A distribution ships a soul — never someone
  else's memories.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import click

_MANIFEST_NAME = "polyrob.profile.yaml"

_DEFAULT_DIST_OWNED = ("characters", "skills", "cron", "mcp.json", _MANIFEST_NAME)
_CONFIG_FILE = "config.yaml"
_SOUL_FILE = "soul.md"

#: Paths a manifest may NEVER claim (user-owned, or outside the profile).
_USER_OWNED = frozenset({".env", "auth.json", "wallet", "data", "logs",
                         "profile.yaml", "active_profile"})


def _git_env() -> dict:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # never prompt for credentials
    env.setdefault("GIT_CONFIG_NOSYSTEM", "1")
    # A distribution source string is attacker-controlled ("install my profile:
    # <url>"). git's default protocol set includes ext::/fd::, which run an
    # arbitrary command (ext::sh -c '…') at clone time = RCE. Pin the allowlist
    # to real transports only, mirroring tools/self_env/tool.py.
    env["GIT_ALLOW_PROTOCOL"] = "file:git:http:https:ssh"
    return env


def _fetch_source(source: str, tmp: Path) -> Path:
    """Materialize *source* (git URL with optional ``#ref``, or local dir)."""
    ref = None
    if "#" in source and not Path(source).exists():
        source, _, ref = source.partition("#")
    src_path = Path(source).expanduser()
    if src_path.is_dir():
        dest = tmp / "src"
        shutil.copytree(src_path, dest,
                        ignore=shutil.ignore_patterns(".git"), symlinks=False,
                        ignore_dangling_symlinks=True)
        return dest
    dest = tmp / "clone"
    # `--` ends option parsing so an option-shaped source can't inject a git flag.
    cmd = ["git", "clone", "--depth", "50", "--", source, str(dest)]
    r = subprocess.run(cmd, env=_git_env(), capture_output=True, text=True,
                       timeout=300)
    if r.returncode != 0:
        raise click.ClickException(f"git clone failed: {r.stderr.strip()[:400]}")
    if ref:
        # An option-shaped ref (e.g. "--upload-pack=…") would be read as a git
        # flag. `--` can't help here (it forces pathspec, not a commit-ish), so
        # reject a leading dash outright — a real ref never starts with one.
        if ref.startswith("-"):
            raise click.ClickException(f"invalid ref {ref!r}")
        r = subprocess.run(["git", "checkout", "--detach", ref], cwd=str(dest),
                           env=_git_env(), capture_output=True, text=True,
                           timeout=120)
        if r.returncode != 0:
            raise click.ClickException(
                f"git checkout {ref!r} failed: {r.stderr.strip()[:400]}")
    shutil.rmtree(dest / ".git", ignore_errors=True)
    return dest


def _read_manifest(src: Path) -> dict:
    import yaml
    f = src / _MANIFEST_NAME
    if not f.is_file():
        raise click.ClickException(
            f"{_MANIFEST_NAME} not found in {src} — not a profile distribution")
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
    except Exception as exc:
        raise click.ClickException(f"broken {_MANIFEST_NAME}: {exc}")
    if not isinstance(data, dict) or not str(data.get("name") or "").strip():
        raise click.ClickException(f"{_MANIFEST_NAME} must declare a name")
    return data


def _owned_paths(manifest: dict) -> list:
    """Validated distribution-owned paths (defaults + manifest extras)."""
    owned = list(_DEFAULT_DIST_OWNED)
    extra = manifest.get("distribution_owned") or []
    if not isinstance(extra, list):
        raise click.ClickException("distribution_owned must be a list")
    for raw in extra:
        rel = str(raw).strip().strip("/")
        p = Path(rel)
        if not rel or p.is_absolute() or ".." in p.parts:
            raise click.ClickException(
                f"manifest declares a path outside the profile root: {raw!r}")
        if p.parts[0] in _USER_OWNED:
            raise click.ClickException(
                f"manifest may not claim the user-owned path {raw!r} "
                f"(.env/auth.json/wallet/data are never distribution-owned)")
        if rel not in owned:
            owned.append(rel)
    return owned


def _check_polyrob_requires(manifest: dict) -> None:
    req = str(manifest.get("polyrob_requires") or "").strip()
    if not req:
        return
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version

        from core.version import get_version
        if Version(get_version()) not in SpecifierSet(req, prereleases=True):
            click.echo(f"Warning: this POLYROB is v{get_version()} but the "
                       f"distribution wants {req} — proceeding anyway.", err=True)
    except click.ClickException:
        raise
    except Exception:
        click.echo(f"Warning: could not evaluate polyrob_requires {req!r}.", err=True)


def _copy_owned(src: Path, home: Path, owned: list, *, replace: bool) -> list:
    """Copy owned paths src -> home. With replace=True, an existing owned path
    is removed first (update semantics). Returns what landed."""
    landed = []
    for rel in owned:
        s = src / rel
        if not s.exists():
            continue
        d = home / rel
        if replace and d.exists():
            shutil.rmtree(d) if d.is_dir() else d.unlink()
        if s.is_dir():
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
        landed.append(rel)
    return landed


def _install_soul(src: Path, home: Path, instance_id: str) -> bool:
    soul = src / _SOUL_FILE
    if not soul.is_file():
        return False
    dest = home / "data" / "identity" / instance_id / _SOUL_FILE
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(soul, dest)
    return True


def _record_distribution(home: Path, manifest: dict, source: str) -> None:
    """Remember where this profile came from (for ``update``)."""
    import yaml
    meta_file = home / "profile.yaml"
    meta = {}
    if meta_file.is_file():
        try:
            meta = yaml.safe_load(meta_file.read_text(encoding="utf-8")) or {}
        except Exception:
            meta = {}
    meta.setdefault("name", manifest["name"])
    meta["description"] = str(manifest.get("description") or meta.get("description") or "")
    meta["distribution"] = {
        "source": source,
        "version": str(manifest.get("version") or ""),
    }
    meta_file.write_text(yaml.safe_dump(meta, sort_keys=False), encoding="utf-8")


def _print_env_requires(manifest: dict, home: Path) -> None:
    reqs = manifest.get("env_requires") or []
    if not isinstance(reqs, list) or not reqs:
        return
    click.echo("This distribution needs environment keys (set them in "
               f"{home / '.env'}):")
    for r in reqs:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        required = "required" if r.get("required") else "optional"
        desc = str(r.get("description") or "")
        click.echo(f"  - {name} ({required}) {desc}")


def install_profile(source: str, name: str = None, force: bool = False) -> dict:
    from core.profiles import (InvalidProfileNameError, is_safe_profile_name,
                               profiles_root)
    with tempfile.TemporaryDirectory(prefix="polyrob-dist-") as tmp:
        src = _fetch_source(source, Path(tmp))
        manifest = _read_manifest(src)
        owned = _owned_paths(manifest)
        _check_polyrob_requires(manifest)
        target = (name or str(manifest["name"])).strip()
        if not is_safe_profile_name(target):
            raise InvalidProfileNameError(target)
        home = profiles_root() / target
        if home.exists() and not force:
            raise click.ClickException(
                f"profile '{target}' already exists — pass --force to replace "
                f"its distribution-owned files (user data is kept either way)")

        home.mkdir(parents=True, exist_ok=True)
        for sub in ("characters", "skills", "data"):
            (home / sub).mkdir(exist_ok=True)
        landed = _copy_owned(src, home, owned, replace=force)
        if (src / _CONFIG_FILE).is_file() and (force or not (home / _CONFIG_FILE).exists()):
            shutil.copy2(src / _CONFIG_FILE, home / _CONFIG_FILE)
            landed.append(_CONFIG_FILE)
        if _install_soul(src, home, target):
            landed.append(f"data/identity/{target}/{_SOUL_FILE}")
        if not (home / ".env").exists():
            (home / ".env").write_text(
                f"# POLYROB profile '{target}' (installed distribution)\n"
                f"POLYROB_INSTANCE_ID={target}\n", encoding="utf-8")
        _record_distribution(home, manifest, source)
        return {"home": home, "name": target, "manifest": manifest, "landed": landed}


def update_profile(name: str, force_config: bool = False) -> dict:
    import yaml

    from core.profiles import profiles_root
    home = profiles_root() / name
    if not home.is_dir():
        raise click.ClickException(f"profile '{name}' does not exist")
    meta = {}
    try:
        meta = yaml.safe_load((home / "profile.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    dist = meta.get("distribution") or {}
    source = str(dist.get("source") or "").strip()
    if not source:
        raise click.ClickException(
            f"profile '{name}' was not installed from a distribution "
            f"(no source recorded in profile.yaml)")
    with tempfile.TemporaryDirectory(prefix="polyrob-dist-") as tmp:
        src = _fetch_source(source, Path(tmp))
        manifest = _read_manifest(src)
        owned = _owned_paths(manifest)
        _check_polyrob_requires(manifest)
        landed = _copy_owned(src, home, owned, replace=True)
        # config.yaml: distribution-owned but the USER's live tuning is
        # preserved unless they explicitly ask for the new one.
        if (src / _CONFIG_FILE).is_file() and (force_config or not (home / _CONFIG_FILE).exists()):
            shutil.copy2(src / _CONFIG_FILE, home / _CONFIG_FILE)
            landed.append(_CONFIG_FILE)
        from core.env_file import read_env_file
        try:
            env = read_env_file(home / ".env")
        except Exception:
            env = {}
        instance_id = env.get("POLYROB_INSTANCE_ID") or name
        if _install_soul(src, home, instance_id):
            landed.append(f"data/identity/{instance_id}/{_SOUL_FILE}")
        _record_distribution(home, manifest, source)
        return {"home": home, "manifest": manifest, "landed": landed}


@click.command("install")
@click.argument("source")
@click.option("--name", default=None, metavar="NAME",
              help="Install under a different profile name.")
@click.option("--force", is_flag=True, default=False,
              help="Replace an existing profile's distribution-owned files.")
def install_cmd(source, name, force):
    """Install a profile distribution from a git URL (append #ref to pin) or dir."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()
    from core.profiles import ProfileError
    try:
        result = install_profile(source, name=name, force=force)
    except ProfileError as exc:
        raise click.ClickException(str(exc))
    m = result["manifest"]
    click.echo(f"Installed profile '{result['name']}' "
               f"(v{m.get('version') or '?'}) at {result['home']}")
    if result["landed"]:
        click.echo("Installed: " + ", ".join(result["landed"]))
    _print_env_requires(m, result["home"])
    click.echo(f"Activate with: polyrob -P {result['name']}")


@click.command("update")
@click.argument("name")
@click.option("--force-config", is_flag=True, default=False,
              help="Also replace config.yaml with the distribution's version.")
def update_cmd(name, force_config):
    """Re-fetch a profile's distribution and replace its owned files.

    User data (.env, auth.json, wallet, everything under data/) is never touched.
    """
    result = update_profile(name, force_config=force_config)
    m = result["manifest"]
    click.echo(f"Updated profile '{name}' to v{m.get('version') or '?'}")
    if result["landed"]:
        click.echo("Replaced: " + ", ".join(result["landed"]))


@click.command("info")
@click.argument("name")
def info_cmd(name):
    """Show a profile's distribution manifest (source, version, env needs)."""
    import yaml

    from core.profiles import profiles_root
    home = profiles_root() / name
    if not home.is_dir():
        raise click.ClickException(f"profile '{name}' does not exist")
    manifest = {}
    mf = home / _MANIFEST_NAME
    if mf.is_file():
        try:
            manifest = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
        except Exception:
            pass
    meta = {}
    try:
        meta = yaml.safe_load((home / "profile.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    dist = meta.get("distribution") or {}
    click.echo(f"Profile     : {name}")
    click.echo(f"Home        : {home}")
    if manifest:
        click.echo(f"Distribution: {manifest.get('name')} v{manifest.get('version') or '?'}")
        click.echo(f"Description : {manifest.get('description') or ''}")
        click.echo(f"Author      : {manifest.get('author') or ''}   "
                   f"License: {manifest.get('license') or ''}")
        click.echo(f"Requires    : polyrob {manifest.get('polyrob_requires') or '(any)'}")
    if dist.get("source"):
        click.echo(f"Source      : {dist['source']}")
    _print_env_requires(manifest, home)
