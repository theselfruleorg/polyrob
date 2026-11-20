"""`/persona` — list the characters, and set the owner's persona setting (042b extraction).

The setter is the ONE body every owner seat calls
(``agents.personality.persona_setting.persona_command``); this file renders the
REPL listing and refreshes the live orchestrator.

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
    """Sorted persona names from ``*.character.json`` files.

    An explicit *characters_dir* isolates tests; otherwise UNIONS every tier of
    the ONE character-dir precedence (``persona_resolver.character_search_dirs``).
    A directory that cannot be read is named by :func:`_unreadable_persona_dirs`
    (the listing shows it), never silently read as empty.
    """
    from agents.personality.persona_setting import persona_names
    return persona_names(characters_dir)[0]


def _unreadable_persona_dirs(characters_dir: Optional[Path] = None) -> List[str]:
    from agents.personality.persona_setting import persona_names
    return persona_names(characters_dir)[1]


def _owner_scope(ctx) -> tuple:
    """The tenant + identity home the persona setting lives in (the REPL's owner
    maps onto the deployment's, as every REPL owner verb does)."""
    try:
        from cli._admin_home import admin_owner_tenant
        uid = admin_owner_tenant(ctx.user_id)
    except Exception:
        uid = ctx.user_id or "local"
    return uid, _resolve_prefs_home_dir(ctx)


def _h_persona(ctx: CommandContext) -> None:
    """List available personas, or set the DEFAULT persona for future sessions.

    Usage:
      /persona                  — the report every seat shows: the active
                                   persona and what the model receives
      /persona list             — the templates AND the characters, with
                                   the ACTIVE one marked
      /persona <name-or-text>   — the owner's persona setting for every NEW
                                   chat on every seat
                                   (``agents/personality/persona_setting.py``,
                                   the one store the session resolver reads):
                                   a template key, else a character slug, else
                                   literal text (threat-scanned)
      /persona reset            — back to the operator's persona

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

    if not args:
        # The ONE report every seat shows (agents/personality/persona_report.py);
        # `/persona list` draws the tables below.
        from agents.personality.persona_setting import persona_command
        uid, home_dir = _owner_scope(ctx)
        ctx.emit(persona_command([], user_id=uid, home_dir=home_dir), title="persona")
        return
    if not (len(args) == 1 and args[0].lower() in ("list", "ls")):
        # The ONE setter every owner seat calls (agents/personality/persona_setting.py);
        # the REPL keeps only the glue that refreshes its live orchestrator.
        from agents.personality.persona_setting import RESET_WORDS, persona_command
        uid, home_dir = _owner_scope(ctx)
        reply = persona_command(args, user_id=uid, home_dir=home_dir)
        ctx.emit(reply, title="persona")
        orch = ctx.orchestrator
        if orch is not None and reply.startswith("✅"):
            try:
                from cli.persona import resolve_cli_persona
                refreshed = resolve_cli_persona(user_id=uid, home_dir=home_dir)
                if refreshed or " ".join(args).strip().lower() in RESET_WORDS:
                    orch._persona_block = refreshed
            except Exception:
                pass
        return

    # /persona list — list BOTH selector namespaces, mark the active one.
    # The listing is built by cli.persona.build_persona_listing, shared verbatim
    # with `polyrob persona list` so the two seats can never drift (F2/F3/F13).
    from cli.persona import build_persona_listing
    from cli.ui import candy
    from cli.ui.theme import style

    uid, home_dir = _owner_scope(ctx)
    listing = build_persona_listing(uid, home_dir, character_names=_list_persona_names())
    template_rows = listing["templates"]
    character_rows = listing["characters"]
    guidance = listing["guidance"] + [
        f"⚠️ I could not read the characters directory {d}." for d in _unreadable_persona_dirs()]

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
