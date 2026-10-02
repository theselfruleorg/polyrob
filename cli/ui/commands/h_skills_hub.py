"""h_skills_hub.py — ``/skills tap|search|update`` (067 P6).

The REPL half of ``polyrob skill tap|search|update``. Every subcommand calls the
same library functions as the CLI (``cli.commands.skill_hub``) and renders the
same lines (``format_listing`` / ``format_update``) — no logic lives here.
Network and install work runs in ``asyncio.to_thread`` so the REPL keeps
rendering. Mutating subcommands (tap add/remove, update) are gated on the local
operator, like ``/skills install``.
"""

from __future__ import annotations

import asyncio
from typing import Callable, List

from cli.ui.commands.registry import CommandContext

_TITLE = "skills"


async def dispatch_hub(ctx: CommandContext, sub: str, rest: List[str],
                       require_local: Callable[[CommandContext, str], bool]) -> None:
    if sub == "tap":
        await _cmd_tap(ctx, rest, require_local)
    elif sub == "search":
        await _cmd_search(ctx, rest)
    elif sub == "update":
        await _cmd_update(ctx, rest, require_local)


async def _cmd_tap(ctx: CommandContext, rest: List[str], require_local) -> None:
    from cli.commands import skill_hub

    action = (rest[0].lower() if rest else "list")
    try:
        if action == "list":
            taps = skill_hub.list_taps()
            ctx.emit("\n".join(f"{t.id:<36} {t.tier:<10}" + (" (default)" if t.default else "")
                               for t in taps), title=_TITLE)
            return
        if action not in ("add", "remove") or len(rest) < 2:
            ctx.emit("Usage: /skills tap [list | add <owner/repo[/subdir]> | remove <owner/repo[/subdir]>]",
                     title=_TITLE)
            return
        if not require_local(ctx, f"tap {action}"):
            return
        if action == "add":
            tap, added = skill_hub.add_tap(rest[1])
            ctx.emit(f"Added tap {tap.id} (community — installs are quarantined)." if added
                     else f"Tap {tap.id} is already known ({tap.tier}).", title=_TITLE)
        else:
            ok = skill_hub.remove_tap(rest[1])
            ctx.emit(f"Removed tap {rest[1]}." if ok else f"No added tap {rest[1]!r}.", title=_TITLE)
    except Exception as exc:
        ctx.emit(f"Tap {action} failed: {exc}", title=_TITLE)


async def _cmd_search(ctx: CommandContext, rest: List[str]) -> None:
    from cli.commands import skill_hub

    refresh = "--refresh" in rest
    query = " ".join(t for t in rest if t != "--refresh")
    try:
        listings = await asyncio.to_thread(skill_hub.search, query, refresh=refresh)
    except Exception as exc:
        ctx.emit(f"Search failed: {exc}", title=_TITLE)
        return
    lines: List[str] = []
    for lst in listings:
        lines += skill_hub.format_listing(lst)
    lines.append("Install with /skills install <tap>/<skill>.")
    ctx.emit("\n".join(lines), title=_TITLE)


async def _cmd_update(ctx: CommandContext, rest: List[str], require_local) -> None:
    from cli.commands import skill_hub

    if not require_local(ctx, "update"):
        return
    name = rest[0] if rest else None
    try:
        reports = await asyncio.to_thread(skill_hub.update_skills, name,
                                          user_id=ctx.user_id or "local")
    except Exception as exc:
        ctx.emit(f"Update failed: {exc}", title=_TITLE)
        return
    lines: List[str] = []
    for rep in reports:
        lines += skill_hub.format_update(rep)
    ctx.emit("\n".join(lines) or "No skills in the lock.", title=_TITLE)
