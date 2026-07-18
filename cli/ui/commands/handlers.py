"""handlers.py — the slash-command handler functions + the default registry.

Each ``_h_*`` handler takes one ``CommandContext`` and renders through the
renderer (Rich table/panel or plain ``print_block``). ``build_default_registry``
wires them; ``default_registry`` is the process-wide singleton. Re-exported via
the package ``__init__`` (D6 — the god-file split).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.runtime_paths import data_dir_or_home

from cli.ui.commands.registry import (
    Command,
    CommandContext,
    CommandRegistry,
    ReplExit,
)


def _print_scrubbed(out, renderable) -> None:
    """Route a direct Rich print through the same secret scrub ``emit`` uses.

    ``CommandContext.emit`` is the documented output choke point, but it only
    takes strings — the handlers' Rich-table branches used to print straight
    to the console, bypassing the scrub (2026-07-12 UI-surface review S2).
    Strings are scrubbed whole; Table cells are scrubbed in place (str cells
    only — Rich cell renderables pass through). Scrub failure never suppresses
    output (same fail-open contract as ``emit``).
    """
    try:
        from rich.table import Table

        from cli.ui.secrets import scrub_secrets
        if isinstance(renderable, str):
            renderable = scrub_secrets(renderable)
        elif isinstance(renderable, Table):
            for column in renderable.columns:
                cells = getattr(column, "_cells", None) or []
                for i, cell in enumerate(cells):
                    if isinstance(cell, str):
                        cells[i] = scrub_secrets(cell)
    except Exception:
        pass
    out.print(renderable)


def _h_help(ctx: CommandContext) -> None:
    from cli.ui import candy

    lines = ["Commands:"]
    reg = ctx.registry or default_registry()
    for cmd in reg.commands():
        invoke = f"/{cmd.name}"
        if cmd.usage:
            invoke = f"{invoke} {cmd.usage}"
        alias_str = ""
        if cmd.aliases:
            alias_str = "  (" + ", ".join(f"/{a}" for a in cmd.aliases) + ")"
        lines.append(f"{candy.GUTTER}{invoke:<28} {cmd.help}{alias_str}")
    ctx.emit("\n".join(lines), title="help")


def _h_exit(ctx: CommandContext) -> None:
    raise ReplExit()


def _h_status(ctx: CommandContext) -> None:
    """Live session status: model, tokens, cost estimate, ctx %, steps, compactions."""
    from cli.ui import candy
    from cli.ui.theme import style

    state = ctx.state
    # Refresh ctx metrics from the live message_manager when available.
    agent = ctx.agent
    if state is not None and agent is not None:
        try:
            state.poll(agent)
        except Exception:
            pass

    console = ctx.console()
    if console is not None and state is not None:
        from rich import box
        from rich.table import Table

        table = Table(box=box.SIMPLE, header_style=style("label"), pad_edge=False,
                      show_edge=False, show_header=False)
        table.add_column("k", style=style("label"))
        table.add_column("v", style=style("value"))
        table.add_row("session", ctx.session_id[:16] or "—")
        table.add_row("model", f"{state.model or '—'} ({state.provider or '—'})")
        table.add_row("status", state.status or "—")
        table.add_row("step", str(state.step))
        table.add_row(
            "tokens",
            f"{state.tokens_in} in · {state.tokens_out} out · {state.tokens_total} total",
        )
        table.add_row("cost (est)", f"${state.cost_estimate_total:.4f}")
        if state.ctx_percent:
            table.add_row(
                "context",
                f"{state.ctx_percent:.0f}% ({state.ctx_tokens}/{state.ctx_max})",
            )
        table.add_row("compactions", str(state.compactions))
        table.add_row("elapsed", f"{state.elapsed():.1f}s")
        _print_scrubbed(console, table)
        return

    # Plain fallback.
    if state is None:
        ctx.emit("(no session state)")
        return
    rows = [
        ("session", ctx.session_id[:16] or "—"),
        ("model", f"{state.model or '—'} ({state.provider or '—'})"),
        ("status", state.status or "—"),
        ("step", str(state.step)),
        ("tokens", f"{state.tokens_in} in · {state.tokens_out} out · {state.tokens_total} total"),
        ("cost (est)", f"${state.cost_estimate_total:.4f}"),
    ]
    if state.ctx_percent:
        rows.append(("context", f"{state.ctx_percent:.0f}% ({state.ctx_tokens}/{state.ctx_max})"))
    rows += [
        ("compactions", str(state.compactions)),
        ("elapsed", f"{state.elapsed():.1f}s"),
    ]
    ctx.emit(candy.kv_lines(rows), title="status")


async def _h_usage(ctx: CommandContext) -> None:
    """Authoritative usage breakdown (DB) with a labelled SessionState fallback.

    CLI sessions skip credit checks, so the ``usage_records`` table is usually
    empty for them — in that case we fall back to the llm_usage-file aggregation
    already accumulated into ``SessionState`` and say which source we show.
    """
    breakdown = await _fetch_breakdown(ctx)

    if breakdown and breakdown.get("by_type"):
        _render_usage_breakdown(ctx, breakdown)
        _maybe_note_estimate_drift(ctx, breakdown)
        return

    # Fallback: SessionState estimate (llm_usage files).
    _render_usage_estimate(ctx)


async def _fetch_breakdown(ctx: CommandContext) -> Optional[Dict[str, Any]]:
    """Pull ``get_session_breakdown`` via the orchestrator's usage_tracker."""
    tracker = None
    orchestrator = ctx.orchestrator
    if orchestrator is None and ctx.task_agent is not None and ctx.session_id:
        try:
            orchestrator = ctx.task_agent.get_orchestrator(ctx.session_id)
        except Exception:
            orchestrator = None
    if orchestrator is not None:
        tracker = getattr(orchestrator, "usage_tracker", None)
    if tracker is None:
        return None
    try:
        return await tracker.get_session_breakdown(ctx.session_id)
    except Exception:
        return None


def _render_usage_breakdown(ctx: CommandContext, breakdown: Dict[str, Any]) -> None:
    from cli.ui import candy
    from cli.ui.theme import style

    console = ctx.console()
    by_type = breakdown.get("by_type", [])
    columns = ["type", "calls", "in", "out", "cache", "credits", "api $", "markup $"]
    rows: List[List[Any]] = []
    for row in by_type:
        tok = row.get("tokens", {})
        rows.append([
            row.get("type", ""),
            row.get("calls", 0),
            tok.get("input", 0),
            tok.get("output", 0),
            tok.get("cached", 0),
            row.get("credits_charged", 0),
            f"${row.get('api_cost_usd', 0):.4f}",
            f"${row.get('markup_usd', 0):.4f}",
        ])
    rows.append([
        "TOTAL", "", "", "", "",
        breakdown.get("total_credits_charged", 0),
        f"${breakdown.get('total_api_cost_usd', 0):.4f}",
        f"${breakdown.get('total_markup_usd', 0):.4f}",
    ])

    if console is not None:
        from rich import box
        from rich.table import Table

        table = Table(box=box.SIMPLE, header_style=style("label"), pad_edge=False, show_edge=False)
        for col in columns:
            table.add_column(col)
        for r in rows:
            table.add_row(*[str(c) for c in r])
        _print_scrubbed(console, "usage (DB — authoritative)")
        _print_scrubbed(console, table)
        return

    text = "usage (DB — authoritative):\n" + candy.table_lines(columns, rows)
    ctx.emit(text, title="usage")


def _maybe_note_estimate_drift(ctx: CommandContext, breakdown: Dict[str, Any]) -> None:
    """Note when the live-bar estimate differs from the authoritative DB cost."""
    state = ctx.state
    if state is None:
        return
    est = state.cost_estimate_total
    db = breakdown.get("total_api_cost_usd", 0) or 0
    if est and abs(est - db) > max(0.0001, db * 0.01):
        ctx.emit(
            f"note: live-bar estimate ${est:.4f} differs from DB api cost ${db:.4f}"
        )


def _render_usage_estimate(ctx: CommandContext) -> None:
    from cli.ui import candy

    state = ctx.state
    if state is None:
        ctx.emit(candy.empty("usage recorded for this session"), title="usage")
        return
    rows = [
        ("tokens", f"{state.tokens_in} in · {state.tokens_out} out · {state.tokens_total} total"),
        ("cost (est)", f"${state.cost_estimate_total:.4f}"),
    ]
    text = (
        "usage (estimate — from llm_usage files; CLI sessions skip DB credit tracking):\n"
        + candy.kv_lines(rows)
    )
    ctx.emit(text, title="usage")


def _h_tools(ctx: CommandContext) -> None:
    """List the agent's registered actions, grouped by tool."""
    from cli.ui import candy
    from cli.ui.theme import style

    actions = _registry_actions(ctx)
    if not actions:
        ctx.emit(
            candy.empty("registered tools", "agent registry unavailable", yet=False),
            title="tools",
        )
        return

    grouped: Dict[str, List[str]] = {}
    for name, action in sorted(actions.items()):
        tool = getattr(action, "tool", None) or "default"
        grouped.setdefault(tool, []).append(name)

    console = ctx.console()
    if console is not None:
        from rich import box
        from rich.table import Table

        table = Table(box=box.SIMPLE, header_style=style("label"), pad_edge=False, show_edge=False)
        table.add_column("tool", style=style("value"))
        table.add_column("actions")
        for tool in sorted(grouped):
            table.add_row(tool, ", ".join(grouped[tool]))
        _print_scrubbed(console, table)
        return

    rows = [[tool, ", ".join(grouped[tool])] for tool in sorted(grouped)]
    ctx.emit(candy.table_lines(["tool", "actions"], rows), title="tools")


def _registry_actions(ctx: CommandContext) -> Dict[str, Any]:
    """Best-effort fetch of the name→RegisteredAction dict from the agent.

    Path: ``agent.controller.registry.registry.actions``.  Tolerant of partial
    stubs / missing layers (returns ``{}``).
    """
    agent = ctx.agent
    if agent is None:
        return {}
    # Try a few plausible attribute paths (the proposal abbreviated this).
    for path in (
        ("controller", "registry", "registry", "actions"),
        ("registry", "registry", "actions"),
        ("controller", "registry", "actions"),
    ):
        obj: Any = agent
        ok = True
        for attr in path:
            obj = getattr(obj, attr, None)
            if obj is None:
                ok = False
                break
        if ok and isinstance(obj, dict):
            return obj
    return {}


def _resolve_prefs_home_dir(ctx: CommandContext) -> Any:
    """Resolve the POLYROB home dir the SAME way /self, /config, /approve do.

    The container config's ``data_dir`` (fallback ``"data"``) — the tree
    ``preferences.toml`` and other identity-tier state actually lives under.
    """
    try:
        cfg = getattr(ctx.container, "config", None) if ctx.container else None
        return data_dir_or_home(getattr(cfg, "data_dir", None))
    except Exception:
        return data_dir_or_home(None)


def _h_toolset(ctx: CommandContext) -> None:
    """List named toolsets, or set the DEFAULT toolset for future sessions.

    Usage:
      /toolset           — list all named toolsets + current session tools
      /toolset <name>    — validate *name* against the named-toolset registry
                           and persist it as the ``session.toolset`` preference

    NOTE (owner-UX P2 T6): live mid-session tool switching is NOT supported —
    the session's Controller/tool registration is fixed at agent-creation
    time, and this command does not attempt to re-register tools on the live
    controller. ``/toolset <name>`` validates + persists the preference; it
    takes effect starting the NEXT session (``session.toolset``'s schema
    ``applies`` is ``"next-session"`` — see ``core/prefs.py``). This session's
    active tool set is unchanged.
    """
    from agents.task.tool_defaults import TOOLSETS, resolve_toolset
    from core.bootstrap import cli_unavailable_tools

    args = ctx.args
    console = ctx.console()

    if args:
        # /toolset <name> — validate + persist as the default for new sessions.
        name = args[0].strip().lower()
        if name not in TOOLSETS:
            valid = ", ".join(sorted(TOOLSETS.keys()))
            ctx.emit(
                f"Unknown toolset: {name!r}. Valid toolsets: {valid}",
                title="toolset",
            )
            return

        home_dir = _resolve_prefs_home_dir(ctx)
        from core.prefs import write_preference
        ok, err = write_preference(home_dir, ctx.user_id or "local", "session.toolset", name)
        if not ok:
            ctx.emit(f"toolset not saved: {err}", title="toolset")
            return

        resolved = resolve_toolset(name)
        unavailable = set(cli_unavailable_tools(resolved))
        available = [t for t in resolved if t not in unavailable]
        ctx.emit(
            f"toolset '{name}' saved (resolves to: {', '.join(available) or '—'}) — "
            "applies to the NEXT session (start a new chat, or "
            f"`polyrob run --toolset {name} <task>`). This session's tool set is "
            "unchanged.",
            title="toolset",
        )
        return

    # /toolset (no arg) — list all named toolsets + current session tools.
    # Current session tool ids: best-effort from registered actions.
    actions = _registry_actions(ctx)
    current_tools: List[str] = sorted(
        {getattr(a, "tool", None) or "default" for a in actions.values()} - {"default"}
    ) if actions else []

    from cli.ui import candy
    from cli.ui.theme import style

    if console is not None:
        from rich import box
        from rich.table import Table

        table = Table(box=box.SIMPLE, header_style=style("label"), pad_edge=False, show_edge=False)
        table.add_column("name", style=style("value"))
        table.add_column("ids")
        table.add_column("CLI-available")
        for ts_name, ts_ids in sorted(TOOLSETS.items()):
            unavail = set(cli_unavailable_tools(ts_ids))
            avail = [t for t in ts_ids if t not in unavail]
            mark = " (unavailable tools pruned)" if unavail else ""
            table.add_row(ts_name, ", ".join(ts_ids), ", ".join(avail) + mark)
        _print_scrubbed(console, table)

        if current_tools:
            _print_scrubbed(console, f"current session tools: {', '.join(current_tools)}")
        else:
            _print_scrubbed(console, "(current session tool set unavailable)")
        _print_scrubbed(
            console,
            "Use: polyrob run --toolset <name> <task>  or set POLYROB_AGENT_TOOLSET=<name>",
        )
    else:
        rows = []
        for ts_name, ts_ids in sorted(TOOLSETS.items()):
            unavail = set(cli_unavailable_tools(ts_ids))
            avail = [t for t in ts_ids if t not in unavail]
            mark = " (unavailable tools pruned)" if unavail else ""
            rows.append([ts_name, ", ".join(ts_ids), ", ".join(avail) + mark])
        lines = [candy.table_lines(["name", "ids", "CLI-available"], rows)]
        if current_tools:
            lines.append(f"current session: {', '.join(current_tools)}")
        lines.append("Use: polyrob run --toolset <name> <task>  or  POLYROB_AGENT_TOOLSET=<name>")
        ctx.emit("\n".join(lines), title="toolset")


def _list_persona_names(characters_dir: Optional[Path] = None) -> List[str]:
    """Return sorted persona names from ``*.character.json`` files.

    Falls back gracefully: returns ``[]`` if the directory cannot be read.
    Accepts an explicit *characters_dir* override for test isolation; otherwise
    uses the canonical ``data/characters/`` path relative to the repo root (same
    heuristic as ``CharacterManager``).
    """
    if characters_dir is None:
        # Mirror CharacterManager: prefer data/characters/ when it contains files,
        # else fall back to the package directory.
        try:
            from pathlib import Path as _Path

            candidate = _Path("data") / "characters"
            if not candidate.exists() or not list(candidate.glob("*.character.json")):
                from agents.personality import character as _char_mod

                candidate = _Path(_char_mod.__file__).parent / "characters"
            characters_dir = candidate
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
      /persona                  — list all available (character) persona names
      /persona <name-or-text>   — set the ``session.persona`` preference: a
                                   known template key (general, research,
                                   coding, social, trading, blank) persists
                                   that key; anything else is treated as
                                   literal persona text

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

        key_candidate = value.lower()
        persisted = key_candidate if key_candidate in TEMPLATES else value

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

        ctx.emit(
            f"persona saved (session.persona = {persisted!r}) — applies to the "
            "NEXT session. This session's active persona is unchanged.",
            title="persona",
        )
        return

    # /persona (no arg) — list all available persona names.
    from cli.ui import candy
    from cli.ui.theme import style

    def _persona_bio(pname: str) -> str:
        try:
            from pathlib import Path as _Path
            import json as _json

            pfile = _Path("data") / "characters" / f"{pname}.character.json"
            if pfile.exists():
                d = _json.loads(pfile.read_text(encoding="utf-8"))
                raw = d.get("bio", "")
                return (raw if isinstance(raw, str) else " ".join(raw))[:80]
        except Exception:
            pass
        return ""

    guidance = [
        "Set the CLI persona with POLYROB_PERSONA=<template> "
        "(general, research, coding, social, trading, blank).",
        "Or use /persona <name-or-text> to set it as your default for "
        "new sessions (applies next session).",
    ]

    if console is not None:
        from rich import box
        from rich.table import Table

        table = Table(box=box.SIMPLE, header_style=style("label"), pad_edge=False, show_edge=False)
        table.add_column("name", style=style("value"))
        table.add_column("bio")
        for pname in names:
            table.add_row(pname, _persona_bio(pname))
        _print_scrubbed(console, table)
        for line in guidance:
            _print_scrubbed(console, line)
    else:
        rows = [[pname, _persona_bio(pname)] for pname in names]
        lines = [candy.table_lines(["name", "bio"], rows)] + guidance
        ctx.emit("\n".join(lines), title="personas")


def _h_sessions(ctx: CommandContext) -> None:
    """List all sessions from the session manager."""
    from cli.ui import candy
    from cli.ui.theme import style

    task_agent = ctx.task_agent
    sm = getattr(task_agent, "session_manager", None) if task_agent is not None else None
    if sm is None:
        ctx.emit("(session manager unavailable)")
        return
    try:
        sessions = sm.get_all_sessions() or []
    except Exception as exc:
        ctx.emit(f"Could not list sessions: {exc}")
        return
    if not sessions:
        ctx.emit(candy.empty("sessions"), title="sessions")
        return

    rows = [
        [
            str(s.get("id", ""))[:16],
            str(s.get("status", "")),
            str(s.get("created_at", ""))[:19],
            _session_model(s),
        ]
        for s in sessions
    ]

    console = ctx.console()
    if console is not None:
        from rich import box
        from rich.table import Table

        table = Table(box=box.SIMPLE, header_style=style("label"), pad_edge=False, show_edge=False)
        for col in ("id", "status", "created", "model"):
            table.add_column(col)
        for r in rows:
            table.add_row(*[str(c) for c in r])
        _print_scrubbed(console, table)
        return

    ctx.emit(candy.table_lines(["id", "status", "created", "model"], rows), title="sessions")


def _session_model(session: Dict[str, Any]) -> str:
    agents = session.get("agents") or []
    if agents and isinstance(agents, list):
        first = agents[0]
        if isinstance(first, dict):
            return str(first.get("model", "") or "")
    return str(session.get("model", "") or "")


def _h_history(ctx: CommandContext) -> None:
    """Show the conversation turns (user + assistant)."""
    from cli.ui import candy
    from cli.ui.identity import agent_display_name
    from cli.ui.theme import style

    convo = ctx.conversation
    turns = list(getattr(convo, "turns", []) or [])
    if not turns:
        ctx.emit(candy.empty("conversation history"), title="history")
        return

    agent_col = agent_display_name()
    rows = [
        [str(i), _truncate(getattr(t, "user", ""), 60), _truncate(getattr(t, "assistant", ""), 60)]
        for i, t in enumerate(turns, 1)
    ]

    console = ctx.console()
    if console is not None:
        from rich import box
        from rich.table import Table

        table = Table(box=box.SIMPLE, header_style=style("label"), pad_edge=False, show_edge=False)
        table.add_column("#", style=style("label"))
        table.add_column("user")
        table.add_column(agent_col)
        for r in rows:
            table.add_row(*r)
        _print_scrubbed(console, table)
        return

    ctx.emit(candy.table_lines(["#", "user", agent_col], rows), title="history")


def _h_clear(ctx: CommandContext) -> None:
    """Clear conversation history, keeping the system prompt."""
    mm = ctx.message_manager
    if mm is None:
        ctx.emit("(no message manager — nothing to clear)")
        return
    try:
        mm.clear_history_keep_system()
    except Exception as exc:
        ctx.emit(f"Could not clear history: {exc}")
        return
    turns = getattr(ctx.conversation, "turns", None)
    if turns is not None:
        try:
            turns.clear()
        except Exception:
            pass
    # Reset the live counters (step/ctx%/tool-calls) so /status + /steps don't show
    # pre-clear values; cumulative session cost is preserved (real spend).
    state = getattr(ctx, "state", None)
    if state is not None and hasattr(state, "reset_after_clear"):
        state.reset_after_clear()
    ctx.emit("History cleared (system prompt kept; live counters reset, session cost preserved).")


async def _h_compact(ctx: CommandContext) -> None:
    """Run LLM-based history compaction (async)."""
    mm = ctx.message_manager
    if mm is None:
        ctx.emit("(no message manager — nothing to compact)")
        return
    ctx.emit("compacting history… (this may take a moment)")
    try:
        result = await mm.llm_compact_history()
    except Exception as exc:
        ctx.emit(f"Compaction failed: {exc}")
        return
    if result:
        ctx.emit("History compacted.")
    else:
        ctx.emit("Compaction skipped (nothing to compact / not needed).")


def _current_model_tuple(ctx: CommandContext) -> Optional[tuple]:
    """The (provider, model) the session is ACTUALLY running, for the picker's
    'keep' default — so it reflects the live model (incl. a fallback) rather than
    a separately re-resolved default that can diverge from what's running."""
    agent = ctx.agent
    if agent is not None:
        m = getattr(agent, "model_name", None)
        p = getattr(agent, "llm_provider", None)
        if m and p:
            return (p, m)
    state = ctx.state
    if state is not None and getattr(state, "model", ""):
        return (getattr(state, "provider", "") or "", state.model)
    return None


async def _pick_model_interactive(preselect: Optional[tuple]):
    """Run the arrow-key/fuzzy model selector, correct in every REPL mode.

    Three cases:
    * Persistent REPL (default): the prompt_toolkit ``Application`` owns the
      screen. Drive its embedded :class:`~cli.ui.model_selector.ReplPicker`
      (``app._picker``) — a conditional list above the input, resolved via a
      Future. It runs on the event loop and never blocks stdin or routes through
      patch_stdout's ``StdoutProxy`` (that was the old picker's missing-menu bug).
    * Legacy/plain REPL or tests (no app / no picker): call the ASYNC standalone
      selector, which opens its own throwaway ``Application`` via
      ``await app.run_async()`` (TTY-safe — a non-TTY caller gets the resolved
      default without prompting). It must be the async variant: this coroutine
      already runs inside the REPL's event loop, so the SYNC ``run_standalone``
      (which uses ``asyncio.run()``) would crash with "cannot be called from a
      running event loop".

    Fail-open to the async standalone selector so any prompt_toolkit hiccup can
    never make ``/model`` unusable.
    """
    from cli.ui.model_selector import run_standalone_async

    try:
        from prompt_toolkit.application.current import get_app_or_none

        app = get_app_or_none()
        picker = getattr(app, "_picker", None) if app is not None else None
        if picker is not None and getattr(app, "is_running", False):
            from modules.llm.available_models import available_models, steer_notes
            from cli.ui.model_selector import _default_idx, _resolved_default

            choices = available_models()
            if not choices:
                return None
            default = preselect or _resolved_default(None, choices)
            return await picker.open(choices, _default_idx(choices, default), steer_notes())
    except Exception:
        pass
    return await run_standalone_async(preselect=preselect)


async def _h_model(ctx: CommandContext) -> None:
    """Swap the running session's model live + persist as the new-session default.

    No args launches an interactive picker; all paths (explicit args,
    ``provider/model`` shorthand, and the picker) converge on the same
    persist+swap logic below.
    """
    from cli.config_store import (
        set_default_model, check_provider_model, resolve_model_alias, _provider_for_model,
        env_default_override_note,
    )

    args = ctx.args
    if not args:
        picked = await _pick_model_interactive(_current_model_tuple(ctx))
        if not picked:
            ctx.emit("Cancelled.")
            return
        provider, model = picked
    elif len(args) == 1 and "/" in args[0]:
        provider, model = args[0].split("/", 1)
    elif len(args) == 1:
        # No slash, single token: the only valid shape left is a `model_aliases`
        # name (B6, e.g. `/model fav`) — expand it to (provider, model). Provider
        # may be missing from the alias value (bare-model alias); infer it the same
        # way an explicit `-m <model>` does.
        alias = resolve_model_alias(args[0])
        if not alias or not alias[1]:
            ctx.emit(
                f"Unknown alias '{args[0]}'. "
                "Usage: /model <provider> <model>, /model <provider>/<model>, "
                "or define it under model_aliases."
            )
            return
        alias_provider, model = alias
        provider = alias_provider or _provider_for_model(model)
        if not provider:
            ctx.emit(f"Alias '{args[0]}' has no provider and none could be inferred from '{model}'.")
            return
    elif len(args) >= 2:
        provider, model = args[0], args[1]
    else:
        ctx.emit("Usage: /model <provider> <model>")
        return
    # Validate the provider (an unknown one is silently dropped on the next launch, so
    # 'set' would be a lie) — matches the `polyrob model set-default` twin.
    known, warning = check_provider_model(provider, model)
    if not known:
        ctx.emit(f"Unknown provider '{provider}'. Try /model <provider> <model> with a known provider.")
        return
    if warning:
        ctx.emit(warning)
    set_default_model(provider, model)
    # Honest persistence: an env pin (CHAT_/DEFAULT_PROVIDER) outranks cli.json for
    # new sessions, so don't claim "saved as the default" without the caveat.
    override = env_default_override_note(provider)

    def _with_note(msg: str) -> str:
        return f"{msg}\n{override}" if override else msg

    agent = ctx.agent
    if agent is not None and hasattr(agent, "swap_model"):
        res = await agent.swap_model(provider, model)
        if res.get("ok"):
            # Repaint the frame/toolbar NOW: SessionState.model is only set from an
            # LLMCall event when still unset, so without this the bar would show the
            # PRE-swap model until (and if) a later turn happened to reset it.
            if ctx.state is not None:
                ctx.state.model = res.get("model") or model
                ctx.state.provider = res.get("provider") or provider
            ctx.emit(_with_note(
                f"Model swapped live: {model} ({provider}). Also saved as the default for new sessions."))
            return
        ctx.emit(_with_note(
            f"Saved default {model} ({provider}); live swap failed ({res.get('error')}); "
            "current session keeps its model."))
        return
    ctx.emit(_with_note(
        f"Default model set: {model} ({provider}). Applies to NEW sessions (no live session to swap)."))


def _h_cwd(ctx: CommandContext) -> None:
    """Show the session's workspace directory."""
    try:
        from agents.task.path import pm

        path = pm().get_workspace_dir(ctx.session_id, ctx.user_id)
        ctx.emit(str(path), title="cwd")
    except Exception as exc:
        ctx.emit(f"Could not resolve workspace dir: {exc}")


async def _h_memory(ctx: CommandContext) -> None:
    """Show the active cross-session memory provider, or search it.

    ``/memory``               → show the active provider (FTS / RAG / hybrid / none).
    ``/memory search <query>`` → cross-session recall over the active provider.
    """
    try:
        from modules.memory.registry import get_memory_registry

        provider = get_memory_registry().active()
        name = getattr(provider, "name", None) if provider is not None else None
        external = bool(getattr(provider, "is_external", False)) if provider is not None else False

        # ---- /memory search <query> -----------------------------------------
        if ctx.args and str(ctx.args[0]).lower() == "search":
            query = " ".join(ctx.args[1:]).strip()
            if not query:
                ctx.emit("Usage: /memory search <query>", title="memory")
                return
            if provider is None or not external or not callable(getattr(provider, "search", None)):
                ctx.emit(
                    "No searchable memory backend active (recall disabled).",
                    title="memory",
                )
                return
            hits = await provider.search(query, user_id=ctx.user_id or "local", limit=10)
            text = hits.strip() if isinstance(hits, str) else str(hits or "").strip()
            if not text:
                from cli.ui import candy
                ctx.emit(candy.empty(f"matches for {query!r}", yet=False), title="memory")
                return
            ctx.emit(text, title="memory")
            return

        # ---- /memory (no args) — active provider name (legacy) ---------------
        if not name or not external:
            ctx.emit("No external memory backend active (recall disabled).", title="memory")
            return
        ctx.emit(f"Active memory provider: {name}", title="memory")
    except Exception as exc:
        ctx.emit(f"Could not resolve memory provider: {exc}", title="memory")


def _resolve_memory_backend_name() -> str:
    """Best-effort name of the active cross-session memory provider, or 'none'."""
    try:
        from modules.memory.registry import get_memory_registry

        provider = get_memory_registry().active()
        name = getattr(provider, "name", None) if provider is not None else None
        external = bool(getattr(provider, "is_external", False)) if provider is not None else False
        if name and external:
            return str(name)
    except Exception:
        pass
    return "none"


def _session_info_rows(ctx: CommandContext) -> list:
    """Build the (key, value) rows for ``/session`` — the identity snapshot.

    Composes the existing resolvers (instance/owner/memory/workspace) fail-open
    per field so a missing service degrades to a placeholder, never raises.
    """
    from core.instance import (
        FRAMEWORK_NAME,
        resolve_instance_id,
        resolve_owner_principal,
    )

    state = ctx.state
    model = getattr(state, "model", "") or "—"
    provider = getattr(state, "provider", "") or "—"

    owner = resolve_owner_principal() or "unbound (local owner)"

    workspace = "—"
    try:
        from agents.task.path import pm

        workspace = str(pm().get_workspace_dir(ctx.session_id, ctx.user_id))
    except Exception:
        pass

    autonomy_on = False
    try:
        from agents.task.constants import local_mode_enabled

        autonomy_on = bool(local_mode_enabled())
    except Exception:
        pass

    rows = [
        ("framework", FRAMEWORK_NAME),
        ("instance", resolve_instance_id()),
        ("owner", owner),
        ("user", ctx.user_id or "local"),
        ("session", ctx.session_id or "—"),
        ("model", f"{model} ({provider})"),
        ("memory", _resolve_memory_backend_name()),
        ("autonomy", "on" if autonomy_on else "off"),
        ("workspace", workspace),
    ]
    return rows


def _h_session(ctx: CommandContext) -> None:
    """Full session identity snapshot: polyrob framework / instance, owner, user,
    session id, model, memory backend, autonomy, workspace.

    Distinct from ``/status`` (live token/cost metrics) — this is the static
    identity card. Fail-open per field.
    """
    from cli.ui import candy
    from cli.ui.theme import style

    rows = _session_info_rows(ctx)
    console = ctx.console()
    if console is not None:
        from rich import box
        from rich.table import Table

        table = Table(box=box.SIMPLE, header_style=style("label"), pad_edge=False,
                      show_edge=False, show_header=False)
        table.add_column("k", style=style("label"))
        table.add_column("v", style=style("value"))
        for k, v in rows:
            table.add_row(k, str(v))
        _print_scrubbed(console, table)
        return
    ctx.emit(candy.kv_lines(rows), title="session")


def _autonomy_snapshot(user_id: str, data_dir: str = "data") -> dict:
    """Gather autonomy loop flags + cron jobs + open goals into structured data.

    Pure + fail-open: each store read is independently guarded so a missing/locked
    DB degrades to a captured exception instead of raising into the REPL. Shared by
    ``autonomy_status_lines`` (legacy plain-line shape) and ``_h_autonomy`` (candy
    grammar) so both read the same snapshot without duplicating the store reads.
    """
    import os
    from agents.task.constants import AutonomyConfig, local_mode_enabled

    flags = [
        ("self-wake", AutonomyConfig.self_wake_enabled()),
        ("goals", AutonomyConfig.goals_enabled()),
        ("curator", AutonomyConfig.curator_enabled()),
        ("cron-run-loop", AutonomyConfig.cron_run_loop()),
        ("background-review", AutonomyConfig.background_review_enabled()),
    ]

    cron_jobs = None
    cron_error: Optional[Exception] = None
    try:
        from cron.service import CronService
        from cron.jobs import CronJobStore
        cron_jobs = CronService(CronJobStore(os.path.join(data_dir, "cron.db"))).list_jobs(user_id=user_id)
    except Exception as e:  # fail-open: stores may not exist yet
        cron_error = e

    open_goals = None
    goals_error: Optional[Exception] = None
    try:
        from agents.task.goals.board import GoalBoard
        goals = GoalBoard(os.path.join(data_dir, "goals.db")).list(user_id=user_id)
        open_goals = [g for g in goals if getattr(g, "status", "") not in ("done", "cancelled")]
    except Exception as e:
        goals_error = e

    return {
        "local_mode": local_mode_enabled(),
        "flags": flags,
        "cron_jobs": cron_jobs,
        "cron_error": cron_error,
        "open_goals": open_goals,
        "goals_error": goals_error,
    }


def autonomy_status_lines(user_id: str, data_dir: str = "data") -> list:
    """Build the /autonomy report: enabled loops + scheduled cron jobs / open goals.

    Legacy plain-line shape, kept byte-compatible for existing callers/tests (see
    ``tests/unit/cli/ui/test_commands_autonomy.py``) — ``_h_autonomy`` renders its
    own candy-grammar view from ``_autonomy_snapshot`` instead of reusing these
    lines, so this function's return format is free to stay untouched.
    """
    snap = _autonomy_snapshot(user_id, data_dir)

    lines = [f"local mode (POLYROB_LOCAL): {'on' if snap['local_mode'] else 'off'}"]
    lines.append("loops: " + ", ".join(f"{n}={'on' if v else 'off'}" for n, v in snap["flags"]))

    if snap["cron_error"] is not None:
        lines.append(f"cron jobs: (unavailable: {snap['cron_error']})")
    else:
        jobs = snap["cron_jobs"]
        lines.append(f"cron jobs: {len(jobs)}")
        for j in jobs[:10]:
            when = j.next_run_at.isoformat() if j.next_run_at else "-"
            lines.append(f"  - {j.id} [{j.status}] {j.schedule_spec} -> {when}: {j.task[:48]}")

    if snap["goals_error"] is not None:
        lines.append(f"goals (open): (unavailable: {snap['goals_error']})")
    else:
        lines.append(f"goals (open): {len(snap['open_goals'])}")

    return lines


def _h_autonomy(ctx: CommandContext) -> None:
    """Show autonomy loop state + scheduled cron jobs / open goals (read-only)."""
    from cli.ui import candy

    data_dir = "data"
    try:
        cfg = getattr(ctx.container, "config", None)
        data_dir = data_dir_or_home(getattr(cfg, "data_dir", None))
    except Exception:
        pass

    snap = _autonomy_snapshot(ctx.user_id or "local", data_dir)

    rows = [("local mode", "on" if snap["local_mode"] else "off")]
    rows.extend((name, "on" if val else "off") for name, val in snap["flags"])
    lines = [candy.kv_lines(rows), ""]

    if snap["cron_error"] is not None:
        lines.append(f"{candy.GUTTER}(unavailable: {snap['cron_error']})")
    else:
        jobs = snap["cron_jobs"]
        lines.append(candy.section(f"cron jobs ({len(jobs)})"))
        for j in jobs[:10]:
            when = j.next_run_at.isoformat() if j.next_run_at else "-"
            preview = (j.task or "")[:48]
            lines.append(candy.status_line(j.status, f"{j.id} {j.schedule_spec} -> {when}: {preview}"))

    lines.append("")
    if snap["goals_error"] is not None:
        lines.append(f"{candy.GUTTER}(unavailable: {snap['goals_error']})")
    else:
        open_goals = snap["open_goals"]
        lines.append(candy.section(f"goals (open: {len(open_goals)})"))

    ctx.emit("\n".join(lines), title="autonomy")


def _h_verbose(ctx: CommandContext) -> None:
    """Toggle reasoning/expand verbosity on the renderer.

    Phase 2 left a ``(+N lines — /verbose)`` collapse hint in blocks.py; this
    flips ``renderer.verbose`` which the renderer consults when rendering steps
    (so the collapse is actually controllable).
    """
    renderer = ctx.renderer
    if renderer is None:
        ctx.emit("(no renderer)")
        return
    current = bool(getattr(renderer, "verbose", False))
    new = not current
    try:
        renderer.verbose = new
    except Exception:
        ctx.emit("This renderer does not support /verbose.")
        return
    ctx.emit(f"Verbose mode {'ON' if new else 'OFF'} (full reasoning, no collapse).")


def _h_quiet(ctx: CommandContext) -> None:
    """Toggle the default tool transcript on the renderer.

    The tool call/result lines are ON by default; ``/quiet`` mutes them for a
    clean chat-only view (dialog + final answer). This is a separate axis from
    ``/verbose`` (the full raw trace) — quieting tools does not affect verbose.
    """
    renderer = ctx.renderer
    if renderer is None:
        ctx.emit("(no renderer)")
        return
    current = bool(getattr(renderer, "show_tools", True))
    new = not current
    try:
        renderer.show_tools = new
    except Exception:
        ctx.emit("This renderer does not support /quiet.")
        return
    if new:
        ctx.emit("Tool transcript ON (tool calls + results shown).")
    else:
        ctx.emit("Tool transcript OFF (muted — chat only). /quiet to restore.")


def _h_steps(ctx: CommandContext) -> None:
    """Re-render the last turn's trace (step blocks, tool lines, lifecycle).

    The renderer keeps a per-turn ring buffer of typed RenderEvents; this
    replays it through the trace layer on demand — the retro counterpart to
    the live ``/verbose`` toggle.
    """
    renderer = ctx.renderer
    if renderer is None or not hasattr(renderer, "render_trace"):
        ctx.emit("(no renderer)")
        return
    count = renderer.render_trace()
    if not count:
        from cli.ui import candy
        ctx.emit(candy.empty("turn recorded", "run a turn first, then /steps"), title="steps")


def _h_resume(ctx: CommandContext) -> None:
    """Replay a session's feed dir through the CURRENT renderer.

    This is visual HISTORY replay (NOT live re-attach — out of scope, §11): we
    read the totally-ordered ``{seq:06d}_{type}*.json`` files and push each one
    through ``events.normalize`` into the same renderer the REPL uses.
    """
    args = ctx.args
    if not args:
        ctx.emit("Usage: /resume <session-id>")
        return
    target = args[0]

    feed_dir = _resolve_feed_dir(ctx, target)
    if feed_dir is None or not feed_dir.is_dir():
        from cli.ui import candy
        ctx.emit(candy.empty(f"feed dir for session {target}", yet=False), title="resume")
        return

    files = sorted(feed_dir.glob("[0-9]*_*.json"))
    if not files:
        ctx.emit(f"Feed dir for {target} is empty (nothing to replay).")
        return

    ctx.emit(
        f"--- replaying session {target} ({len(files)} feed events) — "
        "visual history, not a live re-attach ---"
    )

    from cli.ui.events import normalize

    renderer = ctx.renderer
    for path in files:
        try:
            event_dict = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        try:
            event = normalize(event_dict)
            if renderer is not None:
                renderer.on_event(event)
        except Exception:
            continue
    ctx.emit(f"--- end of replay ({target}) ---")


def _resolve_feed_dir(ctx: CommandContext, session_id: str) -> Optional[Path]:
    """Resolve a session's feed dir, preferring one that actually has feed files.

    Candidates, in order:
      1. ``pm().get_feed_dir(session_id, user_id)`` — but this CREATES an empty
         dir as a side effect, so it only WINS when it contains feed files.
      2. CLI-store layout: ``./.polyrob/sessions/**/<session_id>/feed``.
      3. CLI-store flat layout: ``./.polyrob/sessions/<session_id>/feed``.

    We return the first candidate that contains ``[0-9]*_*.json`` files; if none
    has files we return the first existing dir (so the caller can report
    "empty"), else ``None`` (so the caller can report "not found").
    """
    candidates: List[Path] = []

    # Consult pm() ONLY when the feed dir already exists with files — calling
    # get_feed_dir() on an unknown session id creates an empty orphan directory
    # as a side effect (get_subdir → get_session_root → ensure_directory_exists).
    # Instead we construct the candidate path from the pm data_root without any
    # mkdir calls and add it only when the dir is already present.
    try:
        from agents.task.path import pm

        _pm = pm()
        clean_id = _pm.clean_session_id(session_id)
        clean_uid = _pm.clean_user_id(ctx.user_id or "local")
        _candidate = _pm.data_root / clean_uid / clean_id / "feed"
        if _candidate.is_dir():
            candidates.append(_candidate)
    except Exception:
        pass

    try:
        root = Path.cwd() / ".polyrob" / "sessions"
        candidates.extend(root.glob(f"*/{session_id}/feed"))
        candidates.append(root / session_id / "feed")
    except Exception:
        pass

    first_existing: Optional[Path] = None
    for candidate in candidates:
        try:
            if not candidate.is_dir():
                continue
            if first_existing is None:
                first_existing = candidate
            if any(candidate.glob("[0-9]*_*.json")):
                return candidate
        except Exception:
            continue
    return first_existing


# ---------------------------------------------------------------------------
# Additional slash command handlers (P1.5 REPL parity)
# ---------------------------------------------------------------------------


def _h_goals(ctx: CommandContext) -> None:
    """Show goals board summary."""
    from cli.ui import candy

    try:
        from agents.task.goals.board import GoalBoard
        from core.runtime_config import get_data_root
        from pathlib import Path

        data_root = Path(get_data_root())
        db_path = data_root / "goals.db"

        if not db_path.exists():
            ctx.emit(
                candy.empty("goals", "GOALS_ENABLED=off or none created", yet=False),
                title="goals",
            )
            return

        board = GoalBoard(str(db_path))
        # Scope to THIS user (matches /autonomy at handlers.py autonomy_status_lines);
        # user_id=None returned every tenant's goals — wrong slice under multi-tenant
        # and inconsistent with the autonomy view. Local runs are user_id="local".
        goals = board.list(user_id=ctx.user_id or "local", limit=10)

        if not goals:
            ctx.emit(candy.empty("goals", "/autonomy shows loop state"), title="goals")
            return

        lines = [candy.status_line(g.status, f"{g.id[:8]}: {g.title[:40]}") for g in goals[:10]]

        if len(goals) >= 10:
            lines.append(f"{candy.GUTTER}… (run `polyrob goals list` for all)")

        ctx.emit("\n".join(lines), title="goals")
    except Exception as e:
        ctx.emit(f"Goals: {e}", title="goals")


def _h_subagents(ctx: CommandContext) -> None:
    """Show delegation capability info + live background delegations for this session."""
    from cli.ui import candy

    try:
        from agents.task.constants import TimeoutConfig

        enabled = TimeoutConfig.get_sub_agents_enabled()
        max_concurrent = TimeoutConfig.get_max_concurrent_sub_agents()
        max_async = TimeoutConfig.get_max_async_sub_agents()

        lines = [candy.kv_lines([
            ("delegation", "enabled" if enabled else "disabled"),
            ("max concurrent", max_concurrent),
            ("max background", max_async),
        ])]

        # Live background (async) delegations tracked by this session's orchestrator —
        # the read-only counterpart to the delegate_task(background=true) tool, so
        # /subagents reflects what's actually running, not just static config.
        records = []
        reg = getattr(ctx.orchestrator, "async_delegation", None)
        if reg is not None:
            try:
                records = list(reg.list())
            except Exception:
                records = []

        lines.append("")
        if records:
            lines.append(candy.section(f"background delegations ({len(records)})"))
            for r in records[:10]:
                status = getattr(r, "status", "")
                goal = _truncate(getattr(r, "goal", "") or "", 48)
                did = str(getattr(r, "delegation_id", "?"))[:8]
                lines.append(candy.status_line(status, f"{did}: {goal}"))
        else:
            lines.append(candy.empty(
                "background delegations", "delegate_task(background=true) starts one"
            ))

        ctx.emit("\n".join(lines), title="subagents")
    except Exception as e:
        ctx.emit(f"Subagents: {e}", title="subagents")


def _h_todos(ctx: CommandContext) -> None:
    """Show todos from the agent's session todo file."""
    import re
    from cli.ui import candy
    from agents.task.path import pm

    # Resolve the SAME file the task tool writes (session-scoped), not ./todo.md in
    # CWD — the agent never writes there, so the old path always showed "no todos".
    todo_file = pm().get_todo_file_path(ctx.session_id, ctx.user_id)
    if not todo_file.exists():
        ctx.emit(candy.empty("todos"), title="todos")
        return

    content = todo_file.read_text()
    items = []
    for line in content.splitlines():
        match = re.match(r'^-\s*\[([ x])\]\s*(.+)$', line.strip())
        if match:
            status, text = match.groups()
            items.append((status.lower() == "x", text.strip()))

    if not items:
        ctx.emit(candy.empty("todos"), title="todos")
        return

    completed = sum(1 for done, _ in items if done)
    total = len(items)
    lines = [f"{completed}/{total} completed"]
    for completed_flag, text in items[:10]:
        state_word = "done" if completed_flag else "open"
        lines.append(candy.status_line(state_word, text[:50]))

    if len(items) > 10:
        lines.append(f"{candy.GUTTER}… ({len(items) - 10} more)")

    ctx.emit("\n".join(lines), title="todos")


def _h_logs(ctx: CommandContext) -> None:
    """Show recent log entries for the session."""
    from agents.task.path import pm

    # Resolve via pm() (the SSOT: data_root/user/session/logs) rather than a
    # hand-rolled CWD glob whose depth never matched the real layout.
    log_dir = pm().get_logs_dir(ctx.session_id, ctx.user_id)

    if not log_dir or not log_dir.exists():
        from cli.ui import candy
        ctx.emit(candy.empty(f"logs for session {ctx.session_id[:12]}", yet=False), title="logs")
        return

    log_files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]
    if not log_files:
        from cli.ui import candy
        ctx.emit(candy.empty(f"log files in {log_dir}", yet=False), title="logs")
        return

    lines = [f"Recent logs (last 3 files):"]
    for f in log_files:
        size = f.stat().st_size
        mtime = f.stat().st_mtime
        from datetime import datetime
        ts = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        lines.append(f"  {f.name} ({size} bytes, {ts})")

    ctx.emit("\n".join(lines))
    ctx.emit("Use `polyrob logs` for full log access")


def _h_export(ctx: CommandContext) -> None:
    """Export current session data."""
    args = ctx.args
    if not args:
        ctx.emit("Usage: /export <format> [output]")
        ctx.emit("  format: json (default) or txt")
        return

    format_type = args[0] if args else "json"
    output = args[1] if len(args) > 1 else None

    from datetime import datetime
    from agents.task.path import pm

    # Resolve the session dir via pm() (works under POLYROB_DATA_DIR too, unlike the
    # old hardcoded <cwd>/.polyrob/sessions glob).
    session_dir = pm().get_session_root(ctx.session_id, ctx.user_id)

    if output is None:
        # Default into the session workspace (SSOT) — the old bare filename dropped
        # the export into the process CWD, where it was easy to lose. An explicit
        # `output` arg is still honoured verbatim (the user's choice).
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        output = str(session_dir / f"{ctx.session_id[:12]}_export.{format_type}")

    # The useful payload is the conversation itself — serialize the turns, not just
    # three metadata fields (the old export was near-empty).
    turns = getattr(ctx.conversation, "turns", None) or []
    turns_data = [
        {"user": getattr(t, "user", ""), "assistant": getattr(t, "assistant", "")}
        for t in turns
    ]
    export_data = {
        "session_id": ctx.session_id,
        "exported_at": datetime.now().isoformat(),
        "session_dir": str(session_dir),
        "turns": turns_data,
    }

    try:
        import json
        if format_type == "json":
            with open(output, "w") as f:
                json.dump(export_data, f, indent=2, default=str)
        else:
            with open(output, "w") as f:
                f.write(f"Session Export: {ctx.session_id[:12]}\n")
                f.write(f"Exported: {export_data['exported_at']}\n")
                for t in turns_data:
                    f.write(f"\n> {t['user']}\n{t['assistant']}\n")

        ctx.emit(f"Exported to {output} ({len(turns_data)} turn(s))")
    except Exception as e:
        ctx.emit(f"Export failed: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


def _parse_window_seconds(arg: str) -> Optional[float]:
    """Parse a window token like '30m', '24h', '7d' -> seconds. None if unset/bad."""
    if not arg:
        return None
    arg = arg.strip().lower()
    try:
        if arg.endswith("m"):
            return float(arg[:-1]) * 60
        if arg.endswith("h"):
            return float(arg[:-1]) * 3600
        if arg.endswith("d"):
            return float(arg[:-1]) * 86400
        return float(arg)  # bare number = seconds
    except Exception:
        return None


def _h_telemetry(ctx: CommandContext) -> None:
    """Cross-session telemetry: autonomy/governance event counts + wallet spend.

    Reads the durable event log (self_wake/cron_run/wallet_spend/tool_denied/…).
    Usage: /telemetry [window]  e.g. `/telemetry 24h`, `/telemetry 7d`.
    """
    import time as _time
    try:
        from agents.task.telemetry.event_log import get_event_log, event_log_enabled
    except Exception as e:
        ctx.emit(f"(event log unavailable: {e})")
        return
    if not event_log_enabled():
        ctx.emit("(telemetry event log disabled — set TELEMETRY_EVENT_LOG_ENABLED=true)")
        return

    window = ctx.args[0] if ctx.args else ""
    secs = _parse_window_seconds(window)
    since_ts = (_time.time() - secs) if secs else None

    log = get_event_log()
    agg = log.aggregate(since_ts=since_ts)
    recent = log.query(since_ts=since_ts, limit=10)

    scope = f"last {window}" if secs else "all time"
    counts = agg.get("counts_by_kind", {})

    from cli.ui import candy
    from cli.ui.theme import style

    console = ctx.console()
    if console is not None:
        from rich import box
        from rich.table import Table

        t = Table(box=box.SIMPLE, header_style=style("label"), pad_edge=False, show_edge=False)
        t.add_column("event kind", style=style("value"))
        t.add_column("count", justify="right")
        for kind, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            t.add_row(kind, str(n))
        t.add_row("total", str(agg.get("total_events", 0)))
        t.add_row("wallet spend", f"${agg.get('wallet_spend_usd', 0.0):.4f}")
        _print_scrubbed(console, f"telemetry — {scope}")
        _print_scrubbed(console, t)
        if recent:
            rt = Table(box=box.SIMPLE, header_style=style("label"), pad_edge=False, show_edge=False)
            rt.add_column("kind")
            rt.add_column("outcome/detail")
            rt.add_column("session", style=style("label"))
            for r in recent:
                a = r.get("attrs", {})
                # memory_* events carry a scrubbed preview instead of an outcome (T4-02)
                detail = (a.get("outcome") or a.get("action") or a.get("reason")
                          or a.get("preview") or "")
                rt.add_row(r["kind"], str(detail), (r.get("session_id") or "")[:12])
            _print_scrubbed(console, "recent events")
            _print_scrubbed(console, rt)
        return

    # Plain fallback.
    lines = [f"telemetry — {scope}:", "", candy.section("totals")]
    for kind, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {kind}: {n}")
    lines.append(f"  total events: {agg.get('total_events', 0)}")
    lines.append(f"  wallet spend: ${agg.get('wallet_spend_usd', 0.0):.4f}")
    if recent:
        recent_rows = []
        for r in recent:
            a = r.get("attrs", {})
            detail = (a.get("outcome") or a.get("action") or a.get("reason")
                      or a.get("preview") or "")
            recent_rows.append([r["kind"], str(detail), (r.get("session_id") or "")[:12]])
        lines += [
            "",
            candy.section("recent events"),
            candy.table_lines(["kind", "outcome/detail", "session"], recent_rows),
        ]
    ctx.emit("\n".join(lines), title="telemetry")


def _h_pending(ctx: CommandContext) -> None:
    """Owner review queue for the agent's self-evolution proposals (T4-06b/T4-07).

    Umbrella over `core.self_evolution` — the same pipeline `polyrob owner
    pending/promote/reject` administers, now reachable without leaving the REPL.
    (NOT the marketplace-install quarantine — that stays under `/skills approve`.)

    Usage:
      /pending                       — list pending proposals (skills + identity notes)
      /pending show <kind> <id>      — full-body review of one proposal (T3-09)
      /pending approve <kind> <id>   — promote to active
                                        (kind: skill | self_context | owner_doc |
                                         contract | pref_change)
      /pending reject <kind> <id>    — reject (archive, recoverable)
    """
    import core.instance as _ci
    from cli.ui import candy
    from core import self_evolution

    uid = (ctx.user_id or "").strip() or "local"
    # The REPL is a trusted local operator surface ({cli,local,repl}); the
    # local=True bypass is the documented owner check for it. A bound owner
    # principal always wins; an unbound local operator IS the owner here.
    if not _ci.is_owner(uid, local=True):
        ctx.emit("(owner-only command — the review queue gates self-evolution)")
        return

    cfg = getattr(ctx.container, "config", None) if ctx.container else None
    home_dir = data_dir_or_home(getattr(cfg, "data_dir", None))
    instance_id = _ci.resolve_instance_id()

    args = list(ctx.args or [])
    if args and args[0].lower() in ("approve", "promote", "reject", "show"):
        if len(args) < 3:
            ctx.emit("usage: /pending show|approve|reject <kind> <id>   "
                     "(kind: skill | self_context | owner_doc | contract | pref_change)")
            return
        verb, kind, item_id = args[0].lower(), args[1], " ".join(args[2:])
        if verb == "show":
            ok, body = self_evolution.show(kind, item_id, user_id=uid,
                                           home_dir=home_dir, instance_id=instance_id)
            ctx.emit(body, title=f"pending {kind}:{item_id}" if ok else "pending")
            return
        fn = self_evolution.reject if verb == "reject" else self_evolution.promote
        ok, msg = fn(kind, item_id, user_id=uid, home_dir=home_dir, instance_id=instance_id)
        ctx.emit(msg, title="pending")
        return

    items = self_evolution.list_pending(uid, home_dir=home_dir, instance_id=instance_id)
    if not items:
        ctx.emit(candy.empty("pending proposals", "/approve <id> reviews one"), title="pending")
        return
    lines = [f"{len(items)} pending proposal(s):"]
    for it in items:
        # pending_kind_label is landing from a parallel wave; fall back to the
        # raw kind so /pending never breaks on a tree without it.
        label = getattr(self_evolution, "pending_kind_label", lambda k: k)(it["kind"])
        lines.append(candy.status_line(
            "pending", f"[{label}] {it['kind']}:{it['id']}  ({it['chars']} chars)"
        ))
        lines.append(f"{candy.GUTTER}  {it['preview']}")
    lines.append("")
    lines.append("approve: /pending approve <kind> <id>    reject: /pending reject <kind> <id>")
    ctx.emit("\n".join(lines), title="pending")


def _h_context(ctx: CommandContext) -> None:
    """Context-assembly transparency (owner-UX P1 T9).

    One line per populated foundation slot (system prompt, runtime identity,
    self_context, project_context, skills, history) with its token count and
    % of context, plus a total + context-limit footer. Reads the LIVE
    session's message manager via ``ctx.message_manager`` (same seam as
    ``/clear``/``/compact``); the rendering itself is the pure
    ``render_context_breakdown`` in ``h_context.py`` — no live session yet
    degrades to a friendly line rather than an error.
    """
    from cli.ui.commands.h_context import render_context_breakdown

    out = render_context_breakdown(ctx.message_manager)
    ctx.emit(out, title="context")


# ---------------------------------------------------------------------------
# Default registry (the built-in command set)
# ---------------------------------------------------------------------------


def build_default_registry() -> CommandRegistry:
    """Build the registry with all built-in commands registered."""
    reg = CommandRegistry()
    reg.register(Command("help", _h_help, "Show this help", aliases=("h", "?")))
    reg.register(Command("exit", _h_exit, "Leave the REPL", aliases=("quit", "q")))
    reg.register(Command("status", _h_status, "Live session status (tokens, cost, ctx)"))
    reg.register(
        Command("usage", _h_usage, "Authoritative usage breakdown (DB / estimate)", aliases=("cost",))
    )
    reg.register(
        Command("telemetry", _h_telemetry,
                "Cross-session event counts + wallet spend (arg: window e.g. 24h)")
    )
    from cli.ui.commands.h_journey import h_journey as _h_journey
    reg.register(
        Command("journey", _h_journey,
                "Timeline: what I did, learned, changed, and my income (arg: window e.g. 24h|7d)",
                usage="[window]", aliases=("recap",))
    )
    from cli.ui.commands.h_finance import h_finance as _h_finance
    reg.register(
        Command("finance", _h_finance,
                "Balance sheet: income, spend, pending, net + runtime cost (arg: days)",
                usage="[days]")
    )
    from cli.ui.commands.h_learn import h_learn as _h_learn
    reg.register(
        Command("learn", _h_learn,
                "Describe a procedure; distill it into a pending skill for review",
                usage="<description>")
    )
    reg.register(Command("tools", _h_tools, "List the agent's registered tools/actions"))
    reg.register(Command(
        "toolset",
        _h_toolset,
        "List named toolsets, or set the default toolset for new sessions",
        usage="[name]",
    ))
    reg.register(Command(
        "persona",
        _h_persona,
        "List available personas, or set the default persona for new sessions",
        usage="[name-or-text]",
    ))
    reg.register(Command("sessions", _h_sessions, "List all known sessions"))
    reg.register(
        Command(
            "replay",
            _h_resume,
            "Replay a session's feed (visual history) — NOT a re-attach; continue a "
            "session with `polyrob run --resume <id>`",
            usage="<session-id>",
            aliases=("resume",),
        )
    )
    reg.register(Command("history", _h_history, "Show this conversation's turns"))
    reg.register(Command("clear", _h_clear, "Clear history (keep the system prompt)"))
    reg.register(Command("compact", _h_compact, "Compact conversation history (runs in background)", aliases=("compress",)))
    reg.register(
        Command(
            "model",
            _h_model,
            "Swap the session model live + persist as default",
            usage="<provider> <model> | <provider>/<model> | <alias> (see model_aliases)",
        )
    )
    reg.register(Command("cwd", _h_cwd, "Show the session workspace directory"))
    reg.register(Command(
        "session", _h_session,
        "Session identity: polyrob/instance, owner, user, model, memory, workspace",
        aliases=("info",),
    ))
    from cli.ui.commands.h_self import h_self
    reg.register(Command(
        "self", h_self,
        "Show the instance identity (SOUL + SELF docs, read-only)",
        aliases=("identity", "soul"),
    ))
    reg.register(Command(
        "memory", _h_memory,
        "Show the memory provider; /memory search <query> to recall cross-session",
        usage="[search <query>]",
    ))
    reg.register(Command("verbose", _h_verbose, "Toggle the live trace (steps, tools, reasoning)"))
    reg.register(Command("quiet", _h_quiet, "Mute/restore the default tool transcript"))
    reg.register(Command("steps", _h_steps, "Show the last turn's steps/tools trace"))
    reg.register(Command(
        "autonomy", _h_autonomy,
        help="show autonomy loops + scheduled cron jobs / open goals",
    ))
    reg.register(Command("goals", _h_goals, "Show goals board summary"))
    reg.register(Command("subagents", _h_subagents, "Show delegation capability info"))
    reg.register(Command("todos", _h_todos, "Show workspace todos from todo.md"))
    reg.register(Command("logs", _h_logs, "Show recent log entries for this session"))
    reg.register(Command(
        "export",
        _h_export,
        "Export current session data",
        usage="<format> [output]",
    ))
    # Capability surfaces (each handler lives in its own cli/ui/commands/h_*.py
    # module so the REPL can reach subsystems the CLI groups already exposed —
    # skills / cron / mcp / kb. Read-only; imported locally to keep registry
    # construction cheap and dependency-light.
    from cli.ui.commands.h_skills import h_skills
    from cli.ui.commands.h_cron import h_cron
    from cli.ui.commands.h_mcp import h_mcp
    from cli.ui.commands.h_kb import h_kb
    from cli.ui.commands.h_pfp import h_pfp

    reg.register(Command(
        "skills", h_skills,
        "List/search skills; manage the install pipeline (list/info/install/approve/remove)",
        usage="[query | list | info <id> | install <spec> | approve <id> | remove <id>]",
    ))
    reg.register(Command(
        "cron", h_cron,
        "List scheduled cron jobs (read-only)",
        aliases=("crons",), usage="[list]",
    ))
    reg.register(Command("mcp", h_mcp, "List configured MCP servers + status", usage="[list]"))
    reg.register(Command(
        "kb", h_kb,
        "List + search the local knowledge base",
        usage="[list [collection] | search <query>]",
    ))
    reg.register(Command(
        "pfp", h_pfp,
        "Show/generate the agent avatar (Mindprint; generation is optional)",
        usage="[status|generate [force]|show]", aliases=("avatar",),
    ))
    reg.register(Command(
        "pending", _h_pending,
        "Review the agent's pending self-evolution proposals (show/approve/reject)",
        usage="[show|approve|reject <kind> <id>]",
    ))

    from cli.ui.commands.h_config import ConfigCtx, cmd_config

    async def _h_config(ctx: CommandContext) -> None:
        # Same home_dir resolution as /self and /pending: the container
        # config's data_dir (fallback "data") — the tree preferences.toml
        # and other identity-tier state actually lives under.
        cfg = getattr(ctx.container, "config", None) if ctx.container else None
        home_dir = data_dir_or_home(getattr(cfg, "data_dir", None))
        config_ctx = ConfigCtx(user_id=ctx.user_id or "local", home_dir=home_dir)
        args = list(ctx.args or [])
        # 018 P2b: bare /config in the persistent app opens the settings picker
        # (the SAME frozen ReplPicker /model uses, fed setting rows). Selection
        # prefills a ready-to-send `/config set KEY …`. Any hiccup — no app, no
        # picker, cancel — falls back to the classic list panel.
        if not args:
            picked = await _config_pick_interactive(config_ctx)
            if picked == "handled":
                return
        out = cmd_config(config_ctx, args)
        ctx.emit(out, title="config")

    async def _config_pick_interactive(config_ctx) -> str:
        """Open the settings picker; returns 'handled' iff a row was picked and
        the input buffer was seeded (nothing to print). Fail-open otherwise."""
        try:
            from prompt_toolkit.application.current import get_app_or_none

            app = get_app_or_none()
            picker = getattr(app, "_picker", None) if app is not None else None
            if picker is None or not getattr(app, "is_running", False):
                return "fallback"
            from cli.ui.config_picker import build_setting_choices, prefill_for

            choices = build_setting_choices(config_ctx.user_id, config_ctx.home_dir)
            if not choices:
                return "fallback"
            sel = await picker.open(
                choices, 0,
                ["Enter seeds a /config set command · ⛨ guarded · ≈ advisory · ↻ restart"])
            if not sel:
                return "fallback"
            _group, key = sel
            from core import config_service
            info = config_service.describe(key, user_id=config_ctx.user_id,
                                           home_dir=config_ctx.home_dir)
            buf = picker.input_buffer
            buf.text = prefill_for(info)
            buf.cursor_position = len(buf.text)
            return "handled"
        except Exception:
            return "fallback"

    reg.register(Command(
        "config", _h_config,
        "View/change preferences and flags (list|get|set|explain|search|check)",
        usage="list [group] | get KEY | set KEY VALUE [--confirm] | "
              "explain KEY | search QUERY | check",
    ))

    from cli.ui.commands.h_approve import ApproveCtx, cmd_approve

    def _h_approve(ctx: CommandContext) -> None:
        # Same home_dir resolution as /config/self/pending: the container
        # config's data_dir (fallback "data").
        cfg = getattr(ctx.container, "config", None) if ctx.container else None
        home_dir = data_dir_or_home(getattr(cfg, "data_dir", None))
        approve_ctx = ApproveCtx(user_id=ctx.user_id or "local", home_dir=home_dir)
        out = cmd_approve(approve_ctx, list(ctx.args or []))
        ctx.emit(out, title="approve")

    reg.register(Command(
        "approve", _h_approve,
        "Manage approval gates (list|add|remove)",
        usage="list | add <action> | remove <action>",
    ))
    reg.register(Command(
        "context", _h_context,
        "Context-assembly breakdown: per-slot token counts + % of context",
    ))
    return reg


_DEFAULT_REGISTRY: Optional[CommandRegistry] = None


def default_registry() -> CommandRegistry:
    """Return the process-wide default registry (built once)."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = build_default_registry()
    return _DEFAULT_REGISTRY


def reset_default_registry() -> None:
    """Reset the process-wide default registry to ``None``.

    Test-isolation seam: call this in teardown so the next test that calls
    ``default_registry()`` gets a freshly built registry rather than sharing
    state left by a previous test that may have registered extra commands or
    mutated the singleton.
    """
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = None
