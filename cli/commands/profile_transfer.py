"""polyrob profile export / import — local backup + restore (W5).

NOT a distribution format (that is ``polyrob profile install``, W6): an export
is a tar.gz snapshot of one profile for backup or moving between the owner's
own machines. Export discipline:

- credential files NEVER enter the archive (``.env``, ``auth.json``, wallet
  material, anything ``is_credential_file`` matches);
- every staged text file is force-scrubbed with the repo's secret-shape SSOT
  (``core.secret_patterns.apply_ssot_shapes``) — "share archives must not emit
  raw keys", regardless of any live-redaction setting;
- staging works on a COPY (symlinks materialized), so the live profile is
  byte-unchanged and redaction can never follow a link back into the source;
- extraction is traversal-guarded (no absolute members, no ``..``, no links).
"""
import shutil
import tarfile
import tempfile
from pathlib import Path

import click

#: Never exported (beyond the is_credential_file name guard).
_EXPORT_EXCLUDE_NAMES = frozenset({".env", "auth.json", "active_profile"})
_EXPORT_EXCLUDE_DIRS = frozenset({"wallet", "logs", "__pycache__"})

#: Only files up to this size are candidates for text scrubbing.
_SCRUB_MAX_BYTES = 5 * 1024 * 1024


def _is_excluded(rel: Path) -> bool:
    from core.security.secret_guard import is_credential_file
    if rel.name in _EXPORT_EXCLUDE_NAMES:
        return True
    if any(part in _EXPORT_EXCLUDE_DIRS for part in rel.parts):
        return True
    try:
        if is_credential_file(rel):
            return True
    except Exception:
        pass
    return False


def _scrub_staged_tree(stage: Path) -> int:
    """Force-redact secret shapes in every staged TEXT file. Returns hits."""
    from core.secret_patterns import apply_ssot_shapes
    hits = 0
    for p in sorted(stage.rglob("*")):
        try:
            if not p.is_file() or p.stat().st_size > _SCRUB_MAX_BYTES:
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # binary — never scrubbed, and DBs may hold anything:
                # they are the owner's own data; the credential FILES are what
                # must never ship.
            scrubbed = apply_ssot_shapes(text)
            if scrubbed != text:
                p.write_text(scrubbed, encoding="utf-8")
                hits += 1
        except Exception:
            continue
    return hits


def export_profile(name: str, out_path: Path) -> dict:
    """Stage → exclude creds → scrub → tar.gz. Returns a summary dict."""
    from core.profiles import ProfileNotFoundError, profiles_root
    home = profiles_root() / name
    if not home.is_dir():
        raise ProfileNotFoundError(name, home)

    excluded = []
    with tempfile.TemporaryDirectory(prefix="polyrob-export-") as tmp:
        stage = Path(tmp) / name

        def _ignore(src, names):
            skip = []
            for n in names:
                rel = (Path(src) / n).relative_to(home)
                if _is_excluded(rel):
                    skip.append(n)
                    excluded.append(str(rel))
            return skip

        # symlinks=False FOLLOWS links => the staged copy is materialized;
        # a dangling link is skipped rather than failing the export.
        shutil.copytree(home, stage, ignore=_ignore, symlinks=False,
                        ignore_dangling_symlinks=True)
        scrub_hits = _scrub_staged_tree(stage)

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(out_path, "w:gz") as tar:
            for p in sorted(stage.rglob("*")):
                tar.add(p, arcname=str(Path(name) / p.relative_to(stage)),
                        recursive=False)
    return {"out": out_path, "excluded": sorted(excluded), "scrubbed_files": scrub_hits}


def _safe_members(tar: tarfile.TarFile):
    """Yield only traversal-safe regular members (safe-extract discipline)."""
    for m in tar.getmembers():
        name = m.name
        if name.startswith("/") or name.startswith("\\"):
            raise click.ClickException(f"refusing archive with absolute member: {name}")
        parts = Path(name).parts
        if ".." in parts:
            raise click.ClickException(f"refusing archive with traversal member: {name}")
        if m.islnk() or m.issym():
            raise click.ClickException(f"refusing archive with link member: {name}")
        if m.isdev():
            raise click.ClickException(f"refusing archive with device member: {name}")
        yield m


def import_profile(archive: Path, name: str = None) -> dict:
    """Extract a profile export into the registry. Refuses to overwrite."""
    from core.profiles import (InvalidProfileNameError, is_safe_profile_name,
                               profiles_root)
    archive = Path(archive)
    if not archive.is_file():
        raise click.ClickException(f"no such archive: {archive}")

    with tarfile.open(archive, "r:gz") as tar:
        members = list(_safe_members(tar))
        tops = {Path(m.name).parts[0] for m in members if Path(m.name).parts}
        if len(tops) != 1:
            raise click.ClickException(
                "archive does not look like a profile export (expected one "
                "top-level directory)")
        src_name = tops.pop()
        target = name or src_name
        if not is_safe_profile_name(target):
            raise InvalidProfileNameError(target)
        dest = profiles_root() / target
        if dest.exists():
            raise click.ClickException(
                f"profile '{target}' already exists — import with --name <other> "
                f"or delete it first")
        with tempfile.TemporaryDirectory(prefix="polyrob-import-") as tmp:
            try:
                # Python 3.12+: the 'data' filter re-rejects links/absolute
                # members and strips high-risk metadata — belt and braces on
                # top of _safe_members.
                tar.extractall(Path(tmp), members=members, filter="data")
            except TypeError:
                tar.extractall(Path(tmp), members=members)
            extracted = Path(tmp) / src_name
            if not extracted.is_dir():
                raise click.ClickException("archive holds no profile directory")
            profiles_root().mkdir(parents=True, exist_ok=True)
            shutil.move(str(extracted), str(dest))
    # A restored profile has no .env (never exported) — re-pin the instance id
    # so the identity axis is intact out of the box.
    env_file = dest / ".env"
    if not env_file.exists():
        env_file.write_text(
            f"# restored from {archive.name} — credentials are never exported;\n"
            f"# re-add provider keys here or run `polyrob init --profile {target}`.\n"
            f"POLYROB_INSTANCE_ID={target}\n", encoding="utf-8")
    return {"home": dest, "name": target}


@click.command("export")
@click.argument("name")
@click.option("-o", "--output", "output", default=None, metavar="FILE",
              help="Output archive path (default: ./<name>-profile.tar.gz)")
def export_cmd(name, output):
    """Export profile NAME as a tar.gz (credentials excluded, secrets scrubbed)."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()
    from core.profiles import ProfileError
    out = Path(output) if output else Path.cwd() / f"{name}-profile.tar.gz"
    try:
        result = export_profile(name, out)
    except ProfileError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Exported profile '{name}' -> {result['out']}")
    if result["excluded"]:
        click.echo("Excluded (credentials/volatile): " + ", ".join(result["excluded"]))
    if result["scrubbed_files"]:
        click.echo(f"Scrubbed secret-shaped strings in {result['scrubbed_files']} file(s).")
    click.echo("Note: .env/auth.json/wallet material never enter an export — "
               "re-add keys on the importing machine.")


@click.command("import")
@click.argument("archive", type=click.Path())
@click.option("--name", "name", default=None, metavar="NAME",
              help="Install under a different profile name.")
def import_cmd(archive, name):
    """Import a profile export (tar.gz) into the local registry."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()
    from core.profiles import ProfileError
    try:
        result = import_profile(Path(archive), name=name)
    except ProfileError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Imported profile '{result['name']}' at {result['home']}")
    click.echo(f"Activate with: polyrob -P {result['name']}")
