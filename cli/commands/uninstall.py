"""`polyrob uninstall` — take it off this machine, honestly.

A self-hosted agent writes four things outside its own package: a data home, a
command shim, a ``PATH`` block in a shell rc, and (optionally) a background
service. Nothing could remove them before 062.

What this verb does itself: the three it can remove safely from inside a
running process — the service, the shim, the ``PATH`` block. What it will not
do: delete the virtualenv it is currently executing from, or the package
manager's copy. It prints the exact command for those instead, because a
process deleting its own interpreter mid-run is the kind of half-success that
leaves a machine in a state nobody can describe.

``--purge`` deletes, and only after printing every path and taking a typed
confirmation.

⚠️ There are TWO homes and they are not the same thing. The **config** home
(``~/.polyrob``) is per-user and holds the keys and the wallet seed. The
**data** home is ``cwd/.polyrob`` BY DESIGN — a per-PROJECT memory
(``core.runtime_paths.resolve_runtime_paths``). So ``--purge`` run from a
random directory would once have deleted that project's memory while calling
it "your data", and left the keys untouched. It now names both, deletes both,
and says plainly that OTHER projects keep their own memory.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import click

_BEGIN = "# >>> polyrob >>>"
_END = "# <<< polyrob <<<"

_RC_FILES = (".zshrc", ".bashrc", ".bash_profile", ".profile",
             ".config/fish/config.fish")


def remove_path_blocks(home: Path) -> list[Path]:
    """Strip the installer's marked PATH block from every shell rc it writes."""
    touched: list[Path] = []
    for rel in _RC_FILES:
        rc = home / rel
        try:
            if not rc.is_file():
                continue
            lines = rc.read_text(encoding="utf-8").splitlines(keepends=True)
            if not any(ln.startswith(_BEGIN) for ln in lines):
                continue
            out, skip = [], False
            for ln in lines:
                if ln.startswith(_BEGIN):
                    skip = True
                    continue
                if ln.startswith(_END):
                    skip = False
                    continue
                if not skip:
                    out.append(ln)
            rc.write_text("".join(out), encoding="utf-8")
            touched.append(rc)
        except OSError:
            continue
    return touched


def find_shim() -> Path | None:
    for candidate in (Path.home() / ".local" / "bin" / "polyrob",
                      Path("/usr/local/bin/polyrob")):
        try:
            if candidate.is_file() and "cli.polyrob" in candidate.read_text(errors="replace"):
                return candidate
        except OSError:
            continue
    return None


def _package_removal_hint() -> str:
    from cli.update.detect import (DOCKER, EDITABLE_GIT, GIT, PIP, PIPX,
                                   detect_install)
    ctx = detect_install()
    if ctx.method == PIPX:
        return "pipx uninstall polyrob"
    if ctx.method == PIP:
        return "python -m pip uninstall polyrob"
    if ctx.method == DOCKER:
        return "remove the container and image"
    if ctx.method in (EDITABLE_GIT, GIT) and ctx.repo_root:
        installer = Path(ctx.repo_root) / "install.sh"
        if installer.is_file():
            return f"bash {installer} --uninstall"
        return f"rm -rf {ctx.repo_root}  # and the virtualenv you installed into"
    return "remove the virtualenv you installed into"


@click.command("uninstall")
@click.option("--purge", is_flag=True, default=False,
              help="Also delete the data home (keys, memory, wallet seed).")
@click.option("--yes", "-y", "assume_yes", is_flag=True, default=False,
              help="Skip the confirmation for --purge (scripted removal).")
def uninstall_cmd(purge: bool, assume_yes: bool):
    """Remove the command, the PATH entry and the service. Keeps your data."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()
    from core.paths import polyrob_home
    from core.runtime_paths import resolve_data_home

    config_home = Path(polyrob_home())
    data_home = Path(resolve_data_home())

    # 1. the background service
    try:
        from cli.commands.service import _platform, service_uninstall
        if _platform() != "unsupported":
            ctx = click.get_current_context()
            try:
                ctx.invoke(service_uninstall)
            except click.ClickException as exc:
                click.echo(f"service: {exc.format_message()}")
    except Exception as exc:  # never block the rest of the removal
        click.echo(f"service: could not remove ({exc})", err=True)

    # 2. the command shim
    shim = find_shim()
    if shim is not None:
        try:
            shim.unlink()
            click.echo(f"Removed {shim}")
        except OSError as exc:
            click.echo(f"Could not remove {shim}: {exc}", err=True)
    else:
        click.echo("No polyrob launcher found on PATH (nothing to remove).")

    # 3. the PATH block
    for rc in remove_path_blocks(Path.home()):
        click.echo(f"Removed the PATH entry from {rc}")

    # 4. the package itself — named, never done from inside it
    click.echo(f"\nRemove the installed package yourself:\n  {_package_removal_hint()}")

    # 5. the two homes
    targets = [("config (keys, settings, the wallet seed)", config_home)]
    if data_home != config_home:
        targets.append(("data for THIS project (memory, goals, identity)", data_home))

    if not purge:
        click.echo("\nYour data is KEPT:")
        for label, path in targets:
            click.echo(f"  {path}   — {label}")
        if data_home != config_home:
            click.echo("  Memory is per-project: other directories keep their own "
                       "`.polyrob` and this verb never sees them.")
        click.echo("  Delete with `polyrob uninstall --purge` once you have read them.")
        return

    click.echo("\n--purge will DELETE:")
    present = []
    for label, path in targets:
        exists = path.exists()
        click.echo(f"  {path}   — {label}{'' if exists else '   (already gone)'}")
        if exists:
            present.append(path)
    click.echo("  The config home holds the agent wallet seed. Funds in that wallet")
    click.echo("  become unreachable unless you exported the mnemonic first")
    click.echo("  (`polyrob wallet export`).")
    if data_home != config_home:
        click.echo("  Memory in OTHER project directories is NOT touched — run this")
        click.echo("  from each one, or delete those `.polyrob` directories yourself.")
    if not present:
        click.echo("  Nothing to delete.")
        return
    if not assume_yes:
        typed = click.prompt("  Type DELETE to confirm", default="", show_default=False)
        if typed.strip() != "DELETE":
            click.echo("Cancelled. Nothing was deleted.")
            return
    for path in present:
        try:
            shutil.rmtree(path)
            click.echo(f"Deleted {path}")
        except OSError as exc:
            click.echo(f"Could not delete {path}: {exc}", err=True)


__all__ = ["uninstall_cmd", "remove_path_blocks", "find_shim"]
