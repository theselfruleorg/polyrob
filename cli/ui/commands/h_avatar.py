"""/avatar — the agent's avatar image from inside the REPL.

Shows the slot (``core.avatar``), sets it from a file, a URL or an NFT, and
clears it — on the same data home as ``polyrob avatar``. Core makes no faces;
the generative collection lives in the ``polyrob-desk`` repo.

113 W4 item 3: the show and clear answers are the chat seat's own
(``tools.avatar_owner.avatar_reply`` / ``avatar_change``); a chat app
sends the photo, and the terminal names the image file instead.
"""
from __future__ import annotations

from cli.ui.commands.registry import CommandContext

_USAGE = ("usage: /avatar [show] · /avatar set <file|url> · "
          "/avatar set nft <chain>:<contract>:<token_id> · /avatar clear · "
          "/avatar push [x] [telegram] [discord]")


async def h_avatar(ctx: CommandContext) -> None:
    args = [a for a in (ctx.args or []) if a]
    sub = args[0].lower() if args else "show"

    from cli.commands import avatar as avatar_cli   # module import so tests can monkeypatch
    try:
        home, instance_id = avatar_cli._home(write=(sub in ("set", "clear", "push")))
    except Exception as e:
        ctx.emit(f"avatar: cannot resolve the data home ({e})", title="avatar")
        return

    if sub in ("show", "status", "info"):
        _show(ctx, home)
    elif sub in ("clear", "push"):
        from tools.avatar_owner import avatar_change
        text, _img = await avatar_change(home, [sub] + args[1:])
        ctx.emit(text, title="avatar")
    elif sub == "set" and len(args) >= 2:
        await _set(ctx, home, instance_id, args[1:])
    else:
        ctx.emit(_USAGE, title="avatar")


def _show(ctx, home) -> None:
    from tools.avatar_owner import avatar_reply
    text, img = avatar_reply(home, [])
    # A chat app sends the image; the terminal names the file.
    ctx.emit(text + (f"\nimage: {img}" if img else ""), title="avatar")


async def _set(ctx, home, instance_id, rest) -> None:
    from core.avatar import AvatarError, describe, set_avatar
    from tools import avatar_sources
    # `set nft <ref>` is the terminal's older spelling of `set nft:<ref>`.
    ref = (f"nft:{rest[1]}" if rest[0].lower() == "nft" and len(rest) >= 2
           else " ".join(rest))
    try:
        data, source = await avatar_sources.image_from_ref(ref, local_files=True)
        st = set_avatar(home, instance_id, data, source=source)
    except AvatarError as e:
        ctx.emit(f"avatar not set: {e}", title="avatar")
        return
    ctx.emit(f"{instance_id} — avatar {describe(st)}\nimage: {st.path}", title="avatar")
