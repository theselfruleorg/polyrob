"""``/avatar`` — the agent's avatar image, on every owner seat.

The avatar is ONE image slot (``core/avatar.py``); core generates no face. The
owner sees it, sets it from a URL or an NFT's image, clears it, or pushes it to
the account pictures (``modules/avatar/push.py``). Only the owner reaches this
verb, and a room refuses it. It lives below the seats (proposal 125 S14):
Telegram and the REPL render the same words; a chat app sends the image, the
terminal names the file.
"""
from typing import List, Optional, Tuple

AVATAR_USAGE = ("`/avatar` — show the avatar\n"
                 "`/avatar set <https-url | ipfs://… | nft:chain:contract:id>` — "
                 "replace it\n"
                 "`/avatar clear` — remove it\n"
                 "`/avatar push [x] [telegram] [discord]` — make my account "
                 "pictures show it (no word = all of them)\n"
                 "(From a file on the box: `polyrob avatar set <file>`.)")


def avatar_reply(data_dir: str, args: List[str]) -> Tuple[str, Optional[str]]:
    """``(text, image_path_or_None)`` for the instance's avatar slot.

    Returns the image path SEPARATELY rather than embedding it, so this stays a
    pure function the tests can read and the one side effect (sending the
    image) lives at the seat.
    """
    from core.avatar import describe, load_avatar
    from core.instance import resolve_instance_id

    if args:
        return (AVATAR_USAGE, None)
    instance_id = resolve_instance_id()
    st = load_avatar(data_dir, instance_id)
    if st.state == "none":
        return (f"*{instance_id}* — avatar not set.\nThat is a normal optional "
                f"state. To give this instance a face:\n" + AVATAR_USAGE, None)
    if st.is_default:
        return (f"*{instance_id}* — avatar: the default polyrob mark.\n"
                f"To give it its own face:\n" + AVATAR_USAGE, str(st.path))
    lines = [f"*{instance_id}* — avatar {describe(st)}"]
    if st.is_set and st.set_at:
        lines.append(f"set {st.set_at}")
    if st.is_set and not st.is_raster:
        lines.append("_an SVG: it arrives as a file, and X/Telegram/Discord need a raster image_")
    return ("\n".join(lines), str(st.path) if st.is_set else None)


async def avatar_change(data_dir: str, args: List[str]) -> Tuple[str, Optional[str]]:
    """``/avatar set <ref>`` | ``/avatar clear`` | ``/avatar push [surfaces]`` ->
    ``(text, image_path_or_None)``."""
    from core.avatar import AvatarError, clear_avatar, describe, set_avatar
    from core.instance import resolve_instance_id

    instance_id = resolve_instance_id()
    sub = args[0].lower() if args else ""
    if sub == "clear":
        removed = clear_avatar(data_dir, instance_id)
        return ((f"*{instance_id}* — avatar cleared; the default mark is back." if removed
                 else f"*{instance_id}* — there was no avatar to clear (it shows the default mark)."), None)
    if sub == "push":
        import asyncio
        from modules.avatar import push as pushmod
        results = await asyncio.to_thread(
            pushmod.push_avatar, data_dir, instance_id, surfaces=args[1:])
        return (f"*{instance_id}* — avatar push\n" + pushmod.render_push(results), None)
    ref = " ".join(args[1:]).strip()
    if sub != "set" or not ref:
        return (AVATAR_USAGE, None)
    from tools import avatar_sources
    try:
        data, source = await avatar_sources.image_from_ref(ref)
        st = set_avatar(data_dir, instance_id, data, source=source)
    except AvatarError as e:
        return (f"The avatar was NOT changed: {e}", None)
    return (f"*{instance_id}* — avatar {describe(st)}\n"
            f"_my X, Telegram and Discord pictures keep the old image until "
            f"`/avatar push`_", str(st.path))
