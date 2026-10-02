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
import json
from typing import Optional, Tuple

import click


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
    from core.avatar import AvatarError, describe, set_avatar, set_avatar_from_file
    from tools import avatar_sources

    if bool(image) == bool(nft_ref):
        raise click.UsageError("give exactly one of IMAGE (a file or URL) or --nft")
    home, instance_id = _home(write=True)
    try:
        if nft_ref:
            chain, contract, token_id = avatar_sources.parse_nft_ref(nft_ref)
            data, source = asyncio.run(avatar_sources.image_from_nft(chain, contract, token_id))
            st = set_avatar(home, instance_id, data, source=source)
        elif "://" in image or image.startswith("data:"):
            data, source = asyncio.run(avatar_sources.image_from_url(image))
            st = set_avatar(home, instance_id, data, source=source)
        else:
            st = set_avatar_from_file(home, instance_id, image)
    except AvatarError as e:
        raise click.ClickException(str(e))
    click.echo(f"{instance_id} — avatar {describe(st)}")
    click.echo(f"image: {st.path}")
    if not st.is_raster:
        click.echo("note: an SVG shows in the console and the registration file, "
                   "but X, Discord and Telegram need a PNG/JPEG — `avatar push` refuses it")


@avatar.command("clear")
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation.")
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


def _pushed_path(home: str, instance_id: str):
    from core.avatar import avatar_dir
    return avatar_dir(home, instance_id) / "pushed.json"


def _load_pushed(home: str, instance_id: str) -> dict:
    try:
        data = json.loads(_pushed_path(home, instance_id).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _record_pushed(home: str, instance_id: str, surface: str, digest: str) -> None:
    pushed = _load_pushed(home, instance_id)
    pushed[surface] = {"hash": digest}
    _pushed_path(home, instance_id).parent.mkdir(parents=True, exist_ok=True)
    _pushed_path(home, instance_id).write_text(json.dumps(pushed, indent=2), encoding="utf-8")


@avatar.command("push")
@click.option("--twitter", "do_twitter", is_flag=True, help="Push to X/Twitter (needs PFP_PUSH_TWITTER=true).")
@click.option("--telegram", "do_telegram", is_flag=True, help="Print Telegram BotFather steps (needs PFP_PUSH_TELEGRAM=true).")
@click.option("--discord", "do_discord", is_flag=True, help="Set the Discord bot avatar (needs PFP_PUSH_DISCORD=true).")
def push_cmd(do_twitter, do_telegram, do_discord):
    """Push the avatar to the agent's surfaces (flag-gated, idempotent)."""
    from core.avatar import load_avatar
    from core.env import bool_env
    from core.remedy import flag_remedy
    from modules.avatar import push as pushmod

    home, instance_id = _home(write=True)
    st = load_avatar(home, instance_id)
    if not st.is_set:
        raise click.ClickException(
            "no avatar to push — set one first (`polyrob avatar set <file|url>`)"
            if st.state == "none" else f"the avatar is unreadable: {st.detail}")
    img = st.path
    if not st.is_raster:
        raise click.ClickException(
            "the avatar is an SVG image; X, Discord and Telegram take only a raster "
            "image (PNG/JPEG/GIF/WebP) — set one with `polyrob avatar set <png>`")

    if not (do_twitter or do_telegram or do_discord):
        do_twitter = do_telegram = do_discord = True  # default: attempt all surfaces (still gated)

    if do_twitter:
        if not bool_env("PFP_PUSH_TWITTER", False):
            click.echo(f"twitter: disabled — {flag_remedy('PFP_PUSH_TWITTER')}")
        else:
            try:
                h = pushmod.sha256_file(img)
                if _load_pushed(home, instance_id).get("twitter", {}).get("hash") == h:
                    click.echo("twitter: unchanged, skipped")
                else:
                    pushmod.push_twitter(img)
                    _record_pushed(home, instance_id, "twitter", h)
                    click.echo("twitter: profile image updated ✓")
            except pushmod.TwitterCredsMissing as e:
                click.echo(f"twitter: {e}")
            except Exception as e:  # fail-open: never crash, always give the manual path
                click.echo(f"twitter: could not set avatar ({e}). Set it manually in the X app; image: {img}")

    if do_discord:
        if not bool_env("PFP_PUSH_DISCORD", False):
            click.echo(f"discord: disabled — {flag_remedy('PFP_PUSH_DISCORD')}")
        else:
            try:
                h = pushmod.sha256_file(img)
                if _load_pushed(home, instance_id).get("discord", {}).get("hash") == h:
                    click.echo("discord: unchanged, skipped")
                else:
                    pushmod.push_discord(img)
                    _record_pushed(home, instance_id, "discord", h)
                    click.echo("discord: bot avatar updated ✓")
            except pushmod.DiscordCredsMissing as e:
                click.echo(f"discord: {e}")
            except Exception as e:  # fail-open: never crash, always give the manual path
                click.echo(f"discord: could not set avatar ({e}). Set it manually in the "
                           f"Discord developer portal (Bot → Icon); image: {img}")

    if do_telegram:
        if not bool_env("PFP_PUSH_TELEGRAM", False):
            click.echo(f"telegram: disabled — {flag_remedy('PFP_PUSH_TELEGRAM')}")
        else:
            click.echo(pushmod.telegram_instructions(img))
