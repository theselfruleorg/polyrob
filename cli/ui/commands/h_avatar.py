"""/avatar — the agent's avatar image from inside the REPL.

Shows the slot (``core.avatar``) and sets it from a file, a URL or an NFT — the
same verbs as ``polyrob avatar``, on the same data home. Core makes no faces;
the generative collection lives in the ``polyrob-desk`` repo.
"""
from __future__ import annotations

import asyncio

from cli.ui.commands.registry import CommandContext

_USAGE = ("usage: /avatar [show] · /avatar set <file|url> · "
          "/avatar set nft <chain>:<contract>:<token_id>")


def h_avatar(ctx: CommandContext) -> None:
    args = [a for a in (ctx.args or []) if a]
    sub = args[0].lower() if args else "show"

    from cli.commands import avatar as avatar_cli   # module import so tests can monkeypatch
    try:
        home, instance_id = avatar_cli._home(write=(sub == "set"))
    except Exception as e:
        ctx.emit(f"avatar: cannot resolve the data home ({e})", title="avatar")
        return

    if sub in ("show", "status", "info"):
        _show(ctx, home, instance_id)
    elif sub == "set" and len(args) >= 2:
        _set(ctx, home, instance_id, args[1:])
    else:
        ctx.emit(_USAGE, title="avatar")


def _show(ctx, home, instance_id) -> None:
    from core.avatar import describe, load_avatar
    st = load_avatar(home, instance_id)
    lines = [f"{instance_id} — avatar {describe(st)}"]
    if st.path is not None:
        lines.append(f"image: {st.path}")
    if st.state == "none" or st.is_default:
        lines.append("set one: /avatar set <file|url>  (or `polyrob avatar set …`)")
    ctx.emit("\n".join(lines), title="avatar")


def _set(ctx, home, instance_id, rest) -> None:
    from core.avatar import AvatarError, describe, set_avatar, set_avatar_from_file
    from tools import avatar_sources
    try:
        if rest[0].lower() == "nft" and len(rest) >= 2:
            chain, contract, token_id = avatar_sources.parse_nft_ref(rest[1])
            data, source = asyncio.run(avatar_sources.image_from_nft(chain, contract, token_id))
            st = set_avatar(home, instance_id, data, source=source)
        elif "://" in rest[0] or rest[0].startswith("data:"):
            data, source = asyncio.run(avatar_sources.image_from_url(rest[0]))
            st = set_avatar(home, instance_id, data, source=source)
        else:
            st = set_avatar_from_file(home, instance_id, " ".join(rest))
    except AvatarError as e:
        ctx.emit(f"avatar not set: {e}", title="avatar")
        return
    except RuntimeError as e:  # asyncio.run inside a running loop
        ctx.emit(f"avatar not set: {e} — use `polyrob avatar set` from a shell", title="avatar")
        return
    ctx.emit(f"{instance_id} — avatar {describe(st)}\nimage: {st.path}", title="avatar")
