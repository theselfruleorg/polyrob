"""`/persona` — list and switch the agent's character (042b extraction).

Moved out of ``handlers.py`` unchanged. That file sits AT its size ratchet,
whose instruction is to extract rather than grow, and every other REPL handler
of this size already lives in its own ``h_*`` module — this one had simply never
been moved.

``handlers.py`` re-exports both names, so
``from cli.ui.commands.handlers import _list_persona_names`` keeps working.
"""
from pathlib import Path
from typing import List, Optional

from cli.ui.commands.registry import CommandContext


def _print_scrubbed(out, renderable) -> None:
    """Lazy shim — the real one lives in ``handlers.py``, which imports THIS
    module, so reaching for it at import time would be circular."""
    from cli.ui.commands.handlers import _print_scrubbed as _impl
    return _impl(out, renderable)


def _resolve_prefs_home_dir(ctx):
    from cli.ui.commands.handlers import _resolve_prefs_home_dir as _impl
    return _impl(ctx)

def _list_persona_names(characters_dir: Optional[Path] = None) -> List[str]:
    """Return sorted persona names from ``*.character.json`` files.

    Falls back gracefully (``[]`` on any read error). An explicit
    *characters_dir* isolates tests; otherwise UNIONS every tier of the ONE
    character-dir precedence (``persona_resolver.character_search_dirs``) —
    the old cwd-relative heuristic never saw a profile's character set.
    """
    if characters_dir is None:
        try:
            from agents.personality.persona_resolver import character_search_dirs
            names = set()
            for d in character_search_dirs():
                try:
                    names.update(p.stem.removesuffix(".character")
                                 for p in d.glob("*.character.json"))
                except Exception:
                    continue
            return sorted(names)
        except Exception:
            return []

    try:
        return sorted(
            # p.name = "researcher.character.json" → stem "researcher.character"
            # We strip the trailing ".character" to get the bare slug.
            p.stem.removesuffix(".character")
            for p in characters_dir.glob("*.character.json")
        )
    except Exception:
        return []


def _h_persona(ctx: CommandContext) -> None:
    """List available personas, or set the DEFAULT persona for future sessions.

    Usage:
      /persona                  — list the templates AND the characters, with
                                   the ACTIVE one marked
      /persona <name-or-text>   — set the ``session.persona`` preference,
                                   resolved through the ONE selector order
                                   (F1): a known template key (general,
                                   research, coding, social, trading, blank),
                                   else a known CHARACTER slug, else literal
                                   persona text

    Before F1 the setter only recognised TEMPLATES, so picking a character name
    the lister had just printed silently persisted that word as the ENTIRE
    ``<identity>`` block and reported success. The setter now shares the
    lister's namespace via ``cli.persona.classify_persona_selector``.

    The literal-text branch is threat-scanned at write time
    (``core.prefs.write_preference`` — same fail-closed scan as the SELF/
    identity docs); a flagged value is REJECTED and the error is surfaced
    verbatim, never silently written.

    NOTE (owner-UX P2 T6): like ``/toolset``, this does not live-patch the
    CURRENT session's already-built system prompt — the ``<identity>`` block
    is assembled once, at agent-creation time
    (``agents/task/agent/message_manager/service.py``), not re-read per turn.
    The persisted preference takes effect starting the NEXT session
    (``session.persona``'s schema ``applies`` is ``"next-session"`` — see
    ``core/prefs.py``). Best-effort: the live orchestrator's ``_persona_block``
    seam (``cli/persona.py``) is still refreshed, so anything freshly created
    within THIS session (e.g. a delegated sub-agent) picks up the new persona
    immediately — but the current turn's system prompt is unchanged.
    """
    args = ctx.args
    console = ctx.console()
    names = _list_persona_names()

    if args:
        value = " ".join(args).strip()
        from agents.task.templates import TEMPLATES
        from cli.persona import classify_persona_selector

        key_candidate = value.lower()
        persisted = key_candidate if key_candidate in TEMPLATES else value
        kind, _name = classify_persona_selector(persisted)

        home_dir = _resolve_prefs_home_dir(ctx)
        from core.prefs import write_preference
        ok, err = write_preference(home_dir, ctx.user_id or "local", "session.persona", persisted)
        if not ok:
            ctx.emit(f"persona not saved: {err}", title="persona")
            return

        # Best-effort: refresh the live orchestrator's persona seam (does NOT
        # rewrite this session's already-built system prompt — see NOTE above).
        orch = ctx.orchestrator
        if orch is not None:
            try:
                from cli.persona import resolve_cli_persona
                refreshed = resolve_cli_persona(user_id=ctx.user_id, home_dir=home_dir)
                if refreshed:
                    orch._persona_block = refreshed
            except Exception:
                pass

        what = {
            "template": f"template {persisted!r}",
            "character": f"character {persisted!r}",
        }.get(kind, f"literal persona text {persisted!r}")
        ctx.emit(
            f"persona saved — {what} (session.persona). Applies to the "
            "NEXT session; this session's active persona is unchanged.",
            title="persona",
        )
        return

    # /persona (no arg) — list BOTH selector namespaces, mark the active one.
    # The listing is built by cli.persona.build_persona_listing, shared verbatim
    # with `polyrob persona list` so the two seats can never drift (F2/F3/F13).
    from cli.persona import build_persona_listing
    from cli.ui import candy
    from cli.ui.theme import style

    listing = build_persona_listing(ctx.user_id or "local", _resolve_prefs_home_dir(ctx),
                                    character_names=names)
    template_rows = listing["templates"]
    character_rows = listing["characters"]
    guidance = listing["guidance"]

    if console is not None:
        from rich import box
        from rich.table import Table

        for title, rows in (("templates", template_rows), ("characters", character_rows)):
            table = Table(box=box.SIMPLE, header_style=style("label"),
                          pad_edge=False, show_edge=False)
            table.add_column(title, style=style("value"))
            table.add_column("bio")
            for row in rows:
                table.add_row(*row)
            _print_scrubbed(console, table)
        for line in guidance:
            _print_scrubbed(console, line)
    else:
        lines = [
            candy.table_lines(["template", "bio"], template_rows),
            candy.table_lines(["character", "bio"], character_rows),
        ] + guidance
        ctx.emit("\n".join(lines), title="personas")
