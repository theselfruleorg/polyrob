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
import shutil
import re
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
    # S2 (2026-09-14): was `dict(os.environ)` — a clone of an ATTACKER-NAMED
    # repo ran with the agent's whole environment (AGENT_WALLET_MASTER_SEED +
    # every API key) in reach of any hook/filter/credential-helper git can be
    # talked into running. `build_git_child_env` inherits an allowlist only and
    # keeps the three hardening flags this function always set (never prompt for
    # credentials; ignore /etc/gitconfig; and pin the protocol allowlist to real
    # transports, because git's default set includes ext::/fd::, which run an
    # arbitrary command (ext::sh -c '…') at clone time = RCE).
    from cli.git_child_env import build_git_child_env
    return build_git_child_env()


def _fetch_source(source: str, tmp: Path) -> Path:
    """Materialize an inert snapshot (git URL pinned to a commit, or local dir)."""
    from cli.profile_review import snapshot
    ref = None
    if "#" in source and not Path(source).exists():
        source, _, ref = source.partition("#")
    src_path = Path(source).expanduser()
    if src_path.is_dir():
        return snapshot(src_path, tmp / "src")
    if not ref or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", ref):
        raise click.ClickException("invalid ref: remote profiles require #<full commit hash>")
    dest = tmp / "clone"
    # Fetch the EXACT pinned commit into an empty repo (SUP-9): a shallow clone of
    # the default branch never contains an older pinned commit, so a fixed depth
    # broke installs of anything but the newest commits. `--` ends option parsing
    # so an option-shaped source cannot inject a git flag. M4 (2026-09-14):
    # `-c core.hooksPath=` — a fetched repo's hooks never run; `core.symlinks=false`
    # stops a symlinked path escaping the temp dir.
    git = ["git", "-c", "core.hooksPath=", "-c", "core.symlinks=false"]

    def _git(args, timeout=300, cwd=None):
        return subprocess.run(git + args, cwd=cwd, env=_git_env(), capture_output=True,
                              text=True, timeout=timeout)

    r = _git(["init", "--quiet", str(dest)], timeout=30)
    if r.returncode != 0:
        raise click.ClickException(f"git init failed: {r.stderr.strip()[:400]}")
    r = _git(["fetch", "--quiet", "--depth", "1", "--", source, ref], cwd=str(dest))
    if r.returncode != 0:
        # A server that refuses a by-sha want: fetch every branch, then select it.
        r = _git(["fetch", "--quiet", "--", source, "+refs/heads/*:refs/remotes/origin/*"],
                 cwd=str(dest), timeout=600)
        if r.returncode != 0:
            raise click.ClickException(f"git fetch failed: {r.stderr.strip()[:400]}")
    r = _git(["checkout", "--quiet", "--detach", ref], cwd=str(dest), timeout=120)
    if r.returncode != 0:
        raise click.ClickException(f"git checkout {ref!r} failed: {r.stderr.strip()[:400]}")
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(dest),
                       env=_git_env(), capture_output=True, text=True, timeout=30)
    if r.returncode or r.stdout.strip().lower() != ref.lower():
        raise click.ClickException("profile checkout does not match the pinned commit")
    return snapshot(dest, tmp / "src")


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
        rel = str(raw).strip().rstrip("/")
        p = Path(rel)
        if not rel or not p.parts or p.is_absolute() or ".." in p.parts:
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


def _record_distribution(home: Path, manifest: dict, source: str, digest: str) -> None:
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
        "sha256": digest,
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


def install_profile(source: str, name: str = None, force: bool = False,
                    *, sha256: str | None = None) -> dict:
    from core.profiles import (InvalidProfileNameError, is_safe_profile_name,
                               profiles_root)
    with tempfile.TemporaryDirectory(prefix="polyrob-dist-") as tmp:
        src = _fetch_source(source, Path(tmp))
        manifest = _read_manifest(src)
        owned = _owned_paths(manifest)
        from cli.profile_review import require_review
        digest = require_review(src, sha256, owned=owned)
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
        _record_distribution(home, manifest, source, digest)
        return {"home": home, "name": target, "manifest": manifest, "landed": landed}


def update_profile(name: str, force_config: bool = False,
                   *, sha256: str | None = None, ref: str | None = None) -> dict:
    import yaml

    from core.profiles import profiles_root, is_safe_profile_name, InvalidProfileNameError
    if not is_safe_profile_name(name):
        raise InvalidProfileNameError(name)
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
    if ref:
        # Move a git distribution to a NEW pinned commit (the recorded source is
        # pinned, so without this an update could never advance).
        base = source.partition("#")[0]
        if Path(base).expanduser().is_dir():
            raise click.ClickException("--ref applies to a git distribution, not a local directory")
        source = f"{base}#{ref.strip()}"
    with tempfile.TemporaryDirectory(prefix="polyrob-dist-") as tmp:
        src = _fetch_source(source, Path(tmp))
        manifest = _read_manifest(src)
        owned = _owned_paths(manifest)
        from cli.profile_review import require_review
        digest = require_review(src, sha256, owned=owned, installed=home)
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
        _record_distribution(home, manifest, source, digest)
        return {"home": home, "manifest": manifest, "landed": landed}


@click.command("install")
@click.argument("source")
@click.option("--name", default=None, metavar="NAME",
              help="Install under a different profile name.")
@click.option("--force", is_flag=True, default=False,
              help="Replace an existing profile's distribution-owned files.")
@click.option("--sha256", default=None, help="SHA-256 of the reviewed profile snapshot.")
def install_cmd(source, name, force, sha256):
    """Install a reviewed profile from a git URL#COMMIT or local directory."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()
    from core.profiles import ProfileError
    try:
        result = install_profile(source, name=name, force=force, sha256=sha256)
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
@click.option("--sha256", default=None, help="SHA-256 of the reviewed profile snapshot.")
@click.option("--ref", default=None, metavar="COMMIT",
              help="Move a git distribution to this full commit hash (review again).")
def update_cmd(name, force_config, sha256, ref):
    """Re-fetch a profile's distribution and replace its owned files.

    A git distribution stays on its pinned commit unless --ref names a new one;
    the changed content is shown and needs a fresh --sha256 approval.
    User data (.env, auth.json, wallet, everything under data/) is never touched.
    """
    result = update_profile(name, force_config=force_config, sha256=sha256, ref=ref)
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
