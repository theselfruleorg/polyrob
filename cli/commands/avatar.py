"""`polyrob avatar` — the agent's avatar: one image the owner sets.

The instance has ONE avatar image (``core.avatar``), set from a file, a URL or
the image of an NFT, and every surface reads it: this CLI, the console, Telegram,
the profile push, the ERC-8004 registration file, the invoice card. Core makes no
faces — the generative collection lives in the ``polyrob-desk`` repo, and a face
it renders arrives here as an ordinary image.

The verbs act on the data home the SERVICE reads (``admin_data_dir``), so an
avatar set over SSH is the one the running agent shows.
"""
from __future__ import annotations

import asyncio
from typing import Optional, Tuple

import click

from cli.commands._options import yes_option


def _home(*, write: Optional[bool]) -> Tuple[str, str]:
    from cli._admin_home import admin_data_dir
    from core.instance import resolve_instance_id
    return admin_data_dir(write=write), resolve_instance_id()


def _show(home: str, instance_id: str) -> None:
    from core.avatar import describe, load_avatar
    st = load_avatar(home, instance_id)
    click.echo(f"{instance_id} — avatar {describe(st)}")
    if st.path is not None:
        click.echo(f"image: {st.path}")
    if st.is_set and st.set_at:
        click.echo(f"set:   {st.set_at}")
    if st.state == "none":
        click.echo("set one: polyrob avatar set <file|url>  ·  "
                   "polyrob avatar set --nft <chain>:<contract>:<token_id>")


@click.group("avatar", invoke_without_command=True)
@click.pass_context
def avatar(ctx):
    """The agent's avatar: show, set (file / URL / NFT), clear, push."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()
    if ctx.invoked_subcommand is None:
        _show(*_home(write=False))


@avatar.command("show")
@click.option("--open", "do_open", is_flag=True, help="Also open the image in the default viewer.")
def show_cmd(do_open):
    """Show whether the avatar is set, where it came from, and its file."""
    home, instance_id = _home(write=False)
    _show(home, instance_id)
    if do_open:
        from core.avatar import avatar_image_path
        path = avatar_image_path(home, instance_id)
        if path is None:
            raise click.ClickException("there is no avatar image to open")
        click.launch(str(path))


@avatar.command("set")
@click.argument("image", required=False)
@click.option("--nft", "nft_ref", default=None, metavar="CHAIN:CONTRACT:TOKEN_ID",
              help="Use the image an NFT's metadata names (e.g. base:0xabc…:42).")
def set_cmd(image, nft_ref):
    """Set the avatar from an image FILE or URL, or from an NFT (--nft)."""
    from core.avatar import AvatarError, describe, set_avatar
    from tools import avatar_sources

    if bool(image) == bool(nft_ref):
        raise click.UsageError("give exactly one of IMAGE (a file or URL) or --nft")
    home, instance_id = _home(write=True)
    try:
        data, source = asyncio.run(avatar_sources.image_from_ref(
            f"nft:{nft_ref}" if nft_ref else image, local_files=True))
        st = set_avatar(home, instance_id, data, source=source)
    except AvatarError as e:
        raise click.ClickException(str(e))
    click.echo(f"{instance_id} — avatar {describe(st)}")
    click.echo(f"image: {st.path}")
    if not st.is_raster:
        click.echo("note: an SVG shows in the console and the registration file, "
                   "but X, Discord and Telegram need a PNG/JPEG — `avatar push` refuses it")


@avatar.command("clear")
@yes_option(help="Skip the confirmation.")
def clear_cmd(yes):
    """Remove the avatar image (the agent then has no face on any surface)."""
    from core.avatar import clear_avatar
    home, instance_id = _home(write=True)
    if not yes:
        click.confirm(f"Remove the {instance_id!r} avatar?", abort=True)
    if clear_avatar(home, instance_id):
        click.echo(f"{instance_id} — avatar removed; it shows the default mark again")
    else:
        click.echo(f"{instance_id} — there was no avatar to remove (it shows the default mark)")


@avatar.command("push")
@click.option("--twitter", "do_twitter", is_flag=True, help="Push to X/Twitter.")
@click.option("--telegram", "do_telegram", is_flag=True, help="Set the Telegram bot photo.")
@click.option("--telegram-chat", "tg_chats", multiple=True, metavar="CHAT_ID",
              help="Set this Telegram group's or channel's photo (the bot must be an admin); repeatable. Alone, it changes that photo only; add --telegram for the bot photo too.")
@click.option("--discord", "do_discord", is_flag=True, help="Set the Discord bot avatar.")
def push_cmd(do_twitter, do_telegram, tg_chats, do_discord):
    """Push the avatar to the agent's accounts (no surface named = each connected one)."""
    from modules.avatar import push as pushmod

    home, instance_id = _home(write=True)
    surfaces = []
    if do_twitter:
        surfaces.append("twitter")
    if do_discord:
        surfaces.append("discord")
    if do_telegram:
        surfaces.append("telegram")
    results = pushmod.push_avatar(home, instance_id, surfaces=surfaces, telegram_chats=tg_chats)
    if results and results[0].status == "refused":
        hint = (" (`polyrob avatar set <file|url>`)"
                if results[0].text.startswith(("no avatar", "the avatar is an SVG")) else "")
        raise click.ClickException(results[0].text + hint)
    click.echo(pushmod.render_push(results))
