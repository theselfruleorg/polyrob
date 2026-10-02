"""``/kb`` slash-command handler — list + search the local knowledge base.

A thin REPL front-end over the same registry routers the one-shot
``polyrob kb`` CLI uses (``cli/commands/kb.py``): it REUSES
``modules.memory.registry.kb_list_sources`` / ``kb_search`` (async) and the
``cli.commands.kb._ensure_memory_backend`` bootstrap so the active memory
provider is wired for the current process. No ingestion / walking / chunking is
re-implemented here.

Contract: ``async def h_kb(ctx) -> None`` — the dispatcher awaits it. ``ctx`` is
a ``cli.ui.commands.registry.CommandContext`` (``ctx.emit``, ``ctx.args``,
``ctx.user_id``). Fail-open like every other handler: a missing backend / raising
router degrades to a friendly one-liner, never a crash out of ``dispatch``.

Usage (the ONE grammar, ``core.kb_grammar`` — Telegram parses it too)
-----
    /kb                         list all KB sources
    /kb list [collection]       list sources (optionally one collection)
    /kb search <query>          search the KB (collection ``default``)
    /kb <words>                 search (the bare form)

All imports are function-local so unit tests can monkeypatch the routers /
gate / bootstrap on their home modules.
"""

from __future__ import annotations

from cli.ui import candy

# The default collection mirrors ``cli/commands/kb.py`` (kb search / add default).
_DEFAULT_COLLECTION = "default"


async def h_kb(ctx) -> None:
    """Handle ``/kb`` — list or search the knowledge base.

    ``/kb`` / ``/kb list [collection]`` → list sources;
    ``/kb search <query>`` → search. Fail-open on every path.
    """
    # ---- enable gate (AutonomyConfig.kb_enabled via cli.commands.kb) --------
    try:
        from cli.commands.kb import _kb_enabled

        if not _kb_enabled():
            from core.remedy import flag_remedy
            ctx.emit(f"Knowledge base is disabled. ({flag_remedy('KB_ENABLED')})", title="kb")
            return
    except Exception as exc:  # gate resolution itself failed → treat as disabled
        ctx.emit(f"Knowledge base is unavailable. ({exc})", title="kb")
        return

    from core.kb_grammar import KB_USAGE, SEARCH, USAGE, parse_kb_args

    user_id = ctx.user_id or "local"
    cmd = parse_kb_args(list(ctx.args or []))
    if cmd.action == USAGE:
        ctx.emit(KB_USAGE, title="kb")
        return
    if cmd.action == SEARCH:
        await _do_search(ctx, query=cmd.query, user_id=user_id)
        return
    await _do_list(ctx, collection=cmd.collection, user_id=user_id)


async def _ensure_backend(ctx) -> None:
    """Wire the active memory provider (fail-open); reuses the one-shot CLI path."""
    try:
        from cli.commands.kb import _ensure_memory_backend

        await _ensure_memory_backend()
    except Exception as exc:
        # Non-fatal: the routers themselves no-op to []/'' if no provider resolved.
        ctx.emit(f"note: memory backend not fully wired ({exc})", title="kb")


async def _do_list(ctx, *, collection, user_id: str) -> None:
    await _ensure_backend(ctx)
    try:
        from modules.memory.registry import kb_list_sources

        sources = await kb_list_sources(user_id=user_id, collection=collection)
    except Exception as exc:
        ctx.emit(f"KB list failed: {exc}", title="kb")
        return

    if not sources:
        hint = f"collection {collection!r}" if collection else ""
        ctx.emit(candy.empty("sources in the knowledge base", hint), title="kb")
        return

    header = f"KB sources ({len(sources)})"
    if collection:
        header += f" — collection {collection!r}"
    lines = [header + ":"]
    lines.extend(candy.bullet(src) for src in sources)
    ctx.emit("\n".join(lines), title="kb")


async def _do_search(ctx, *, query: str, user_id: str) -> None:
    await _ensure_backend(ctx)
    try:
        from modules.memory.registry import kb_search

        result = await kb_search(
            query, user_id=user_id, collection=_DEFAULT_COLLECTION, limit=8
        )
    except Exception as exc:
        ctx.emit(f"KB search failed: {exc}", title="kb")
        return

    if not result:
        ctx.emit(candy.empty(f"results for {query!r}", yet=False), title="kb")
        return

    ctx.emit(f"Results for {query!r}:\n{result}", title="kb")
