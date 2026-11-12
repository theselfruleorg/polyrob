"""``polyrob nft`` — the agent-NFT collection pins, from the terminal.

``polyrob nft profile upgrade [go]`` moves the root-owned admin pin file
(``/etc/polyrob/agent_nft_collections.json``) from profile spec 1 to spec 2
(``agent-nft-profile/2``: an explicit account kind per account, the sale rails under
``standard.erc6551p``). It PRINTS the spec-2 form and a diff of the file and writes nothing;
printing needs no root. With ``go`` it writes, as root only: the old file is kept beside it,
the new one has the old file's mode and owner, and it is written only when both forms parse to
EQUAL profiles (``core.wallet.collection_registry.upgrade_document``).

A spec-1 file keeps working until a later release stops reading it; the chat pins
(``/nft trust``) are written in spec 2 already.
"""
import difflib
import json
import os
import sys
import time

import click


@click.group("nft")
def nft():
    """Agent-NFT collection pins (the admin file)."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()


@nft.group("profile")
def profile():
    """The collection profile schema of the admin pin file."""


def _dump(doc) -> str:
    return json.dumps(doc, indent=2) + "\n"


def _write_as_root(path: str, doc) -> str:
    """Write *doc* over *path* with its mode and owner; the old file is kept. Returns the
    name of the kept copy."""
    st = os.stat(path)
    kept = f"{path}.spec1-{int(time.time())}"
    with open(path, "rb") as src, open(kept, "wb") as dst:
        dst.write(src.read())
    os.chmod(kept, st.st_mode & 0o777)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(_dump(doc))
    os.chmod(tmp, st.st_mode & 0o777)
    os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, path)
    return kept


@profile.command("upgrade")
@click.argument("go", required=False)
@click.option("--path", "path", default=None,
              help="The pin file (default: the admin file /etc/polyrob/agent_nft_collections.json).")
def upgrade(go, path):
    """Print the spec-2 form of the admin pin file and its diff; `go` writes it (root)."""
    from core.wallet import collection_registry as CR
    path = path or CR.REGISTRY_FILE
    if go not in (None, "go"):
        raise click.UsageError(f"unknown word {go!r}: the only word is `go`")
    if not os.path.exists(path):
        click.echo(f"No pin file at {path}: nothing to upgrade.")
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            old = json.load(fh)
        new = CR.upgrade_document(old)
    except (OSError, ValueError, CR.CollectionRegistryError) as exc:
        click.echo(f"❌ {path} cannot be upgraded: {exc}. Nothing was written.", err=True)
        sys.exit(1)
    if new == old:
        click.echo(f"{path} is already {CR.SPEC_V2}. Nothing to do.")
        return
    click.echo(f"The {CR.SPEC_V2} form of {path} (both parse to the same pins):\n")
    click.echo(_dump(new))
    click.echo("".join(difflib.unified_diff(_dump(old).splitlines(True), _dump(new).splitlines(True),
                                            fromfile=f"{path} (now)", tofile=f"{path} ({CR.SPEC_V2})")))
    if go != "go":
        click.echo("Nothing was written. To write it, as root: polyrob nft profile upgrade go")
        return
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0:
        click.echo(f"❌ Writing {path} needs root (the file is root-owned; the agent cannot "
                   f"write its own pins). Nothing was written.", err=True)
        sys.exit(1)
    kept = _write_as_root(path, new)
    CR.load(path)  # read back exactly as the guard reads it
    click.echo(f"✅ Written. The spec-1 file is kept as {kept}.")
