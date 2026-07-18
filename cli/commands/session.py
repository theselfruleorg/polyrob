"""polyrob session commands (P3 cli/commands split)."""
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import click

from cli.ui.events import normalize as _normalize_event
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.state import SessionState


@click.group("session")
def session():
    """Manage task sessions."""
    pass


def find_compaction_checkpoints(data_root: Path, session_id: str) -> List[Path]:
    """Locate `compaction_{n}.json` pre-compaction snapshots for a session (A4).

    Mirrors the multi-pattern session-dir lookup used by `session tail`, then lists
    the checkpoints under `.../data/history`, sorted by their numeric suffix so the
    recovery order is chronological. Returns [] when the session or history is absent.
    """
    data_root = Path(data_root)
    history_dir = None
    patterns = [
        f"*/{session_id}*/data/history",
        f"*/sessions/{session_id}*/data/history",
        f"*/*{session_id}*/data/history",
    ]
    for pattern in patterns:
        for path in data_root.glob(pattern):
            if path.is_dir():
                history_dir = path
                break
        if history_dir:
            break

    if not history_dir:
        return []

    def _n(p: Path) -> int:
        m = re.search(r"compaction_(\d+)\.json$", p.name)
        return int(m.group(1)) if m else 0

    return sorted(history_dir.glob("compaction_*.json"), key=_n)


@session.command("history")
@click.argument("session_id")
@click.option("--dump", type=int, default=None, metavar="N",
              help="Print the full JSON of checkpoint N instead of just listing.")
def session_history(session_id: str, dump):
    """List (or --dump) compaction checkpoints for a session."""
    import json
    from core.bootstrap import setup_project_path, setup_sqlite_compat
    setup_project_path()
    setup_sqlite_compat()

    from agents.task.path import pm

    checkpoints = find_compaction_checkpoints(pm().data_root, session_id)
    if not checkpoints:
        click.echo(f"No compaction checkpoints found for session matching '{session_id}'")
        return

    if dump is not None:
        match = next((p for p in checkpoints
                      if re.search(rf"compaction_{dump}\.json$", p.name)), None)
        if not match:
            click.echo(click.style("[polyrob] ", fg="red") + f"No checkpoint #{dump} (have: "
                       + ", ".join(re.search(r'compaction_(\d+)', p.name).group(1) for p in checkpoints) + ")")
            sys.exit(1)
        try:
            click.echo(json.dumps(json.loads(match.read_text()), indent=2))
        except (json.JSONDecodeError, OSError) as e:
            click.echo(click.style("[polyrob] ", fg="red") + f"Could not read {match.name}: {e}")
            sys.exit(1)
        return

    click.echo(click.style("[polyrob] ", fg="cyan") + f"{len(checkpoints)} checkpoint(s):")
    for p in checkpoints:
        n = re.search(r"compaction_(\d+)", p.name).group(1)
        size = p.stat().st_size
        click.echo(f"  #{n:<4} {p.name:<24} {size:>8} bytes   {p}")
    click.echo("\nInspect one with: " + click.style(f"polyrob session history {session_id} --dump <N>", fg="cyan"))


@session.command("list")
@click.option("--all", "show_all", is_flag=True, help="Show all sessions including completed")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def session_list(show_all: bool, as_json: bool):
    """List recent sessions."""
    asyncio.run(_session_list(show_all, as_json))


async def _session_list(show_all: bool, as_json: bool):
    import logging as _logging
    from core.bootstrap import build_cli_container, setup_project_path, setup_sqlite_compat
    setup_project_path()
    setup_sqlite_compat()

    _logging.disable(_logging.CRITICAL)
    from cli.keys import preflight_or_onboard
    if not preflight_or_onboard(interactive=False):
        sys.exit(1)
    container = await build_cli_container(log_level="ERROR")
    _logging.disable(_logging.NOTSET)

    task_agent = container.get_agent("task_agent")
    if not task_agent:
        click.echo("TaskAgent not available")
        sys.exit(1)

    sessions = task_agent.session_manager.get_all_sessions()

    if not show_all:
        # F9: match the "active" semantics used by the session-limit check and
        # SessionManager.get_active_sessions (session.py:503) — 'suspended' is
        # NOT active there, so excluding it here keeps the CLI's default list in
        # lockstep with what counts against the per-user cap.  Use --all to see
        # suspended/completed/failed/cancelled sessions.
        active_statuses = {"created", "running", "resumed"}
        sessions = [s for s in sessions if s.get("status") in active_statuses]

    if not sessions:
        click.echo("No sessions found.")
        return

    if as_json:
        click.echo(json.dumps(sessions, indent=2, default=str))
        return

    click.echo(f"{'ID':<12} {'Status':<12} {'Task':<40} {'Created'}")
    click.echo("-" * 80)

    for s in sessions:
        # NB: `.get(k, default)` returns None when the key is PRESENT with a null
        # value (default only applies to absent keys), so `(x or default)` is
        # required before slicing — a null task/created_at/id crashed session list.
        sid = (s.get("id") or s.get("session_id") or "?")[:10]
        status = s.get("status") or "?"
        task_str = (s.get("task") or "")[:38]
        created = (s.get("created_at") or "")[:19]

        color = {"running": "green", "created": "cyan", "suspended": "yellow",
                 "completed": "white", "failed": "red", "cancelled": "red"}.get(status, "white")
        click.echo(f"{sid:<12} {click.style(status, fg=color):<21} {task_str:<40} {created}")


@session.command("tail")
@click.argument("session_id")
@click.option("--follow", "-f", is_flag=True,
              help="Keep streaming new feed events as they arrive (Ctrl-C to stop).")
def session_tail(session_id: str, follow: bool):
    """Stream a session's feed (reads from feed directory).

    With --follow, keeps watching the feed directory and renders new events
    live — a second terminal can watch any running session (including
    goal/cron sessions) while it works. 019 P1.
    """
    asyncio.run(_session_tail(session_id, follow=follow))


async def _session_tail(session_id: str, follow: bool = False):
    import json
    from core.bootstrap import setup_project_path, setup_sqlite_compat
    setup_project_path()
    setup_sqlite_compat()

    from agents.task.path import pm

    # Find feed directory — try multiple path patterns
    feed_dir = None
    patterns = [
        f"*/{session_id}*/feed",         # data/task/{user}/{session_id}/feed
        f"*/sessions/{session_id}*/feed", # data/auto/{user}/sessions/{session_id}/feed
        f"*/*{session_id}*/feed",         # partial match
    ]
    for pattern in patterns:
        for path in pm().data_root.glob(pattern):
            if path.is_dir():
                feed_dir = path
                break
        if feed_dir:
            break

    if not feed_dir or not feed_dir.exists():
        click.echo(f"No feed found for session matching '{session_id}'")
        sys.exit(1)

    click.echo(click.style("[polyrob] ", fg="cyan") + f"Tailing feed: {feed_dir}")

    _state = SessionState()
    _renderer = PlainRenderer(state=_state, stream=sys.stdout)

    def _render_file(path) -> bool:
        try:
            data = json.loads(path.read_text())
            event = _normalize_event(data)
            _state.update(event)
            _renderer.on_event(event)
            return True
        except (json.JSONDecodeError, OSError):
            return False

    seen: set[str] = set()
    feed_files = sorted(feed_dir.glob("*.json"))
    for f in feed_files:
        _render_file(f)
        seen.add(f.name)

    if not follow:
        click.echo(click.style("[polyrob] ", fg="cyan")
                   + f"End of feed ({len(feed_files)} entries)")
        return

    # --follow: poll for new feed files. Filenames are sequence-prefixed
    # ({seq:06d}_{type}.json) and write-once (atomic rename), so a sorted
    # name-dedup poll is ordered and race-free — no watcher dependency.
    click.echo(click.style("[polyrob] ", fg="cyan")
               + "Following (Ctrl-C to stop)…")
    try:
        while True:
            await asyncio.sleep(0.5)
            for f in sorted(feed_dir.glob("*.json")):
                if f.name in seen:
                    continue
                _render_file(f)
                seen.add(f.name)
    except (KeyboardInterrupt, asyncio.CancelledError):
        click.echo(click.style("[polyrob] ", fg="cyan") + "Stopped.")


@session.command("cancel")
@click.argument("session_id")
def session_cancel(session_id: str):
    """Cancel a running session."""
    asyncio.run(_session_cancel(session_id))


async def _session_cancel(session_id: str):
    import logging as _logging
    from core.bootstrap import build_cli_container, setup_project_path, setup_sqlite_compat
    setup_project_path()
    setup_sqlite_compat()

    _logging.disable(_logging.CRITICAL)
    from cli.keys import preflight_or_onboard
    if not preflight_or_onboard(interactive=False):
        sys.exit(1)
    container = await build_cli_container(log_level="ERROR")
    _logging.disable(_logging.NOTSET)

    task_agent = container.get_agent("task_agent")
    if not task_agent:
        click.echo("TaskAgent not available")
        sys.exit(1)

    user_id = container.get_service("identity").resolve()
    success = await task_agent.cancel_session(user_id=user_id, session_id=session_id)
    if success:
        click.echo(click.style("[polyrob] ", fg="green") + f"Session {session_id} cancelled")
    else:
        click.echo(click.style("[polyrob] ", fg="red") + f"Failed to cancel session {session_id}")
        sys.exit(1)


@session.command("show")
@click.argument("session_id")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def session_show(session_id: str, as_json: bool):
    """Show detailed information about a session."""
    asyncio.run(_session_show(session_id, as_json))


async def _session_show(session_id: str, as_json: bool):
    import logging as _logging
    from core.bootstrap import build_cli_container, setup_project_path, setup_sqlite_compat
    setup_project_path()
    setup_sqlite_compat()

    _logging.disable(_logging.CRITICAL)
    from cli.keys import preflight_or_onboard
    if not preflight_or_onboard(interactive=False):
        sys.exit(1)
    container = await build_cli_container(log_level="ERROR")
    _logging.disable(_logging.NOTSET)

    task_agent = container.get_agent("task_agent")
    if not task_agent:
        click.echo("TaskAgent not available")
        sys.exit(1)

    session_info = task_agent.session_manager.get_session_info(session_id)
    if not session_info:
        click.echo(click.style("[polyrob] ", fg="red") + f"Session {session_id} not found")
        sys.exit(1)

    if as_json:
        click.echo(json.dumps(session_info, indent=2, default=str))
        return

    sid16 = (session_info.get('id') or session_id)[:16]
    click.echo(f"{click.style(sid16, fg='cyan', bold=True)}: {session_info.get('task') or 'No task'}")
    click.echo(f"  status: {session_info.get('status', '?')}")
    click.echo(f"  created: {session_info.get('created_at', '?')}")
    if session_info.get('updated_at'):
        click.echo(f"  updated: {session_info.get('updated_at')}")
    if session_info.get('model'):
        click.echo(f"  model: {session_info.get('model')}")
    if session_info.get('provider'):
        click.echo(f"  provider: {session_info.get('provider')}")


@session.command("attach")
@click.argument("session_id")
def session_attach(session_id: str):
    """Attach to a running session (NOT YET IMPLEMENTED — use `polyrob run --resume`)."""
    click.echo(click.style("[polyrob] ", fg="yellow")
               + "attach is not yet implemented - continue a session with "
                 "'polyrob run --resume <id>', or 'polyrob session tail <id>' to watch it")
    click.echo("To monitor an existing session, use: polyrob session tail <id>")


@session.command("pause")
@click.argument("session_id")
def session_pause(session_id: str):
    """Pause a running session."""
    asyncio.run(_session_pause(session_id))


async def _session_pause(session_id: str):
    import logging as _logging
    from core.bootstrap import build_cli_container, setup_project_path, setup_sqlite_compat
    setup_project_path()
    setup_sqlite_compat()

    _logging.disable(_logging.CRITICAL)
    from cli.keys import preflight_or_onboard
    if not preflight_or_onboard(interactive=False):
        sys.exit(1)
    container = await build_cli_container(log_level="ERROR")
    _logging.disable(_logging.NOTSET)

    task_agent = container.get_agent("task_agent")
    if not task_agent:
        click.echo("TaskAgent not available")
        sys.exit(1)

    user_id = container.get_service("identity").resolve()
    success = await task_agent.pause_session(user_id=user_id, session_id=session_id)
    if success:
        click.echo(click.style("[polyrob] ", fg="green") + f"Session {session_id} paused")
    else:
        click.echo(click.style("[polyrob] ", fg="red") + f"Failed to pause session {session_id}")
        sys.exit(1)


@session.command("resume")
@click.argument("session_id")
def session_resume(session_id: str):
    """Resume a paused session."""
    asyncio.run(_session_resume(session_id))


async def _session_resume(session_id: str):
    import logging as _logging
    from core.bootstrap import build_cli_container, setup_project_path, setup_sqlite_compat
    setup_project_path()
    setup_sqlite_compat()

    _logging.disable(_logging.CRITICAL)
    from cli.keys import preflight_or_onboard
    if not preflight_or_onboard(interactive=False):
        sys.exit(1)
    container = await build_cli_container(log_level="ERROR")
    _logging.disable(_logging.NOTSET)

    task_agent = container.get_agent("task_agent")
    if not task_agent:
        click.echo("TaskAgent not available")
        sys.exit(1)

    user_id = container.get_service("identity").resolve()
    success = await task_agent.resume_session(user_id=user_id, session_id=session_id)
    if success:
        click.echo(click.style("[polyrob] ", fg="green") + f"Session {session_id} marked resumable")
        click.echo("  Continue execution with: polyrob run --resume " + session_id)
    else:
        click.echo(click.style("[polyrob] ", fg="red") + f"Failed to resume session {session_id}")
        sys.exit(1)


@session.command("export")
@click.argument("session_id")
@click.option("--output", "-o", type=click.Path(), help="Output file path.")
@click.option("--format", type=click.Choice(["json", "txt", "raw", "sharegpt", "openai"]),
              default="json", help="Export format (raw/sharegpt/openai = training formats).")
def session_export(session_id: str, output: Optional[str], format: str):
    """Export a session's data (transcript, messages, artifacts)."""
    asyncio.run(_session_export(session_id, output, format))


def _render_training_format(session_id, session_info, session_dir, fmt) -> dict:
    """Assemble → label-enrich → scrub (fail-closed) → render one session (W1 T4)."""
    from agents.task.path import pm
    from datagen.assemble import (assemble_record, find_memory_db,
                                  load_episode_labels)
    from datagen.formats import FORMATS
    from datagen.scrub import scrub_record

    labels = None
    try:
        # memory.db lives in BotConfig.data_dir — the PARENT of pm().data_root
        # on the local CLI — so resolve it instead of assuming data_root.
        memory_db = find_memory_db(pm().data_root)
        user_id = (session_info or {}).get("user_id") or ""
        if memory_db is not None:
            labels = load_episode_labels(memory_db, str(user_id), session_id)
    except Exception:
        labels = None
    record = assemble_record(Path(session_dir), session_meta=session_info,
                             labels=labels)
    record.session_id = session_id
    scrub_record(record)  # ScrubError propagates: refuse to export unscrubbed
    return FORMATS[fmt](record)


async def _session_export(session_id: str, output: Optional[str], format: str):
    import logging as _logging
    from core.bootstrap import build_cli_container, setup_project_path, setup_sqlite_compat
    setup_project_path()
    setup_sqlite_compat()

    _logging.disable(_logging.CRITICAL)
    from cli.keys import preflight_or_onboard
    if not preflight_or_onboard(interactive=False):
        sys.exit(1)
    container = await build_cli_container(log_level="ERROR")
    _logging.disable(_logging.NOTSET)

    from agents.task.path import pm

    # Fetch session metadata (status/model/task/created) to FOLD INTO the export —
    # previously this was fetched then discarded, so the export omitted it.
    ta = task_agent_or_none(container)
    session_info = ta.session_manager.get_session_info(session_id) if ta else None

    if output is None:
        output = f"{session_id}_export.{format}"

    # Find session directory
    data_root = pm().data_root
    session_dir = None
    patterns = [
        f"*/{session_id}*/",
        f"*/sessions/{session_id}*/",
        f"*/*{session_id}*/",
    ]
    for pattern in patterns:
        for path in data_root.glob(pattern):
            if path.is_dir():
                session_dir = path
                break
        if session_dir:
            break

    if not session_dir:
        click.echo(click.style("[polyrob] ", fg="red") + f"Session directory not found for {session_id}")
        sys.exit(1)

    if format in ("raw", "sharegpt", "openai"):
        from datagen.scrub import ScrubError
        try:
            rendered = _render_training_format(
                session_id, session_info, session_dir, format)
        except ScrubError as e:
            click.echo(click.style("[polyrob] ", fg="red")
                       + f"Export refused (scrub failed): {e}")
            sys.exit(1)
        with open(output, "w") as f:
            json.dump(rendered, f, indent=2, default=str)
        click.echo(click.style("[polyrob] ", fg="green") + f"Exported to {output}")
        return

    export_data = _assemble_export_data(
        session_id, session_info, session_dir, datetime.now().isoformat()
    )

    # Write output
    if format == "json":
        with open(output, "w") as f:
            json.dump(export_data, f, indent=2, default=str)
    else:
        sess = export_data.get("session") or {}
        with open(output, "w") as f:
            f.write(f"Session Export: {session_id}\n")
            f.write(f"Exported: {export_data['exported_at']}\n")
            f.write(f"Directory: {session_dir}\n")
            if sess.get("status"):
                f.write(f"Status: {sess['status']}\n")
            if sess.get("model"):
                f.write(f"Model: {sess['model']}\n")
            if "messages" in export_data:
                f.write(f"Messages: {len(export_data['messages'])}\n")

    click.echo(click.style("[polyrob] ", fg="green") + f"Exported to {output}")


def _summarize_llm_usage(session_dir) -> Optional[dict]:
    """Aggregate the per-call ``llm_usage_*.json`` records under a session dir.

    Returns ``{records, total_tokens, total_cost_estimate}`` or None if there are
    none. Reads the REAL per-call files (the old fallback read a never-written
    ``usage.json``) plus a legacy ``usage.json`` if present. Robust to schema drift:
    the record count is always meaningful; token/cost are best-effort sums.
    """
    d = Path(session_dir) / "data" / "llm_usage"
    if not d.exists():
        return None
    records = []
    for f in sorted(d.glob("llm_usage_*.json")):
        try:
            records.append(json.loads(f.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    legacy = d / "usage.json"
    if legacy.exists():
        try:
            data = json.loads(legacy.read_text())
            records.extend(data if isinstance(data, list) else [data])
        except (json.JSONDecodeError, OSError):
            pass
    if not records:
        return None
    tokens = sum((r.get("token_count") or 0) for r in records if isinstance(r, dict))
    cost = sum((r.get("cost_estimate") or 0) for r in records if isinstance(r, dict))
    return {"records": len(records), "total_tokens": tokens, "total_cost_estimate": round(cost, 6)}


def _assemble_export_data(session_id, session_info, session_dir, exported_at) -> dict:
    """Build the export payload from disk + the fetched session metadata (pure)."""
    session_dir = Path(session_dir)
    export_data = {
        "session_id": session_id,
        "exported_at": exported_at,
        "session_dir": str(session_dir),
    }
    if session_info:
        meta = {
            k: session_info.get(k)
            for k in ("status", "model", "provider", "task", "created_at", "updated_at")
            if session_info.get(k) is not None
        }
        if meta:
            export_data["session"] = meta
    # Read via datagen (current memory/ location first, legacy root fallback) —
    # the old direct read missed memory/message_history.json entirely (W1 T4).
    from datagen.assemble import read_message_history
    history = read_message_history(session_dir)
    if history:
        export_data["messages"] = history
    feed_dir = session_dir / "feed"
    if feed_dir.exists():
        export_data["feed_events"] = len(sorted(feed_dir.glob("*.json")))
    return export_data


def task_agent_or_none(container):
    """Get task_agent from container without noise."""
    import logging as _logging
    _logging.disable(_logging.CRITICAL)
    task_agent = container.get_agent("task_agent")
    _logging.disable(_logging.NOTSET)
    return task_agent


@session.command("artifacts")
@click.argument("session_id")
def session_artifacts(session_id: str):
    """List artifacts (screenshots, downloads, outputs) for a session."""
    from agents.task.path import pm

    data_root = pm().data_root
    session_dir = None
    patterns = [
        f"*/{session_id}*/",
        f"*/sessions/{session_id}*/",
        f"*/*{session_id}*/",
    ]
    for pattern in patterns:
        for path in data_root.glob(pattern):
            if path.is_dir():
                session_dir = path
                break
        if session_dir:
            break

    if not session_dir:
        click.echo(click.style("[polyrob] ", fg="red") + f"Session directory not found for {session_id}")
        sys.exit(1)

    click.echo(f"Artifacts for session {session_id[:16]}:")

    # Screenshots
    screenshots_dir = session_dir / "screenshots"
    if screenshots_dir.exists():
        screenshots = list(screenshots_dir.glob("*"))
        click.echo(f"  Screenshots: {len(screenshots)}")
        for s in screenshots[:10]:  # Show first 10
            click.echo(f"    {s.name}")

    # Workspace files
    workspace_dir = session_dir / "workspace"
    if workspace_dir.exists():
        files = list(workspace_dir.rglob("*"))
        files = [f for f in files if f.is_file()]
        click.echo(f"  Workspace files: {len(files)}")
        for f in files[:10]:
            click.echo(f"    {f.relative_to(workspace_dir)}")


@session.command("costs")
@click.argument("session_id")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def session_costs(session_id: str, as_json: bool):
    """Show cost breakdown for a session."""
    asyncio.run(_session_costs(session_id, as_json))


async def _session_costs(session_id: str, as_json: bool):
    import logging as _logging
    from core.bootstrap import build_cli_container, setup_project_path, setup_sqlite_compat
    setup_project_path()
    setup_sqlite_compat()

    _logging.disable(_logging.CRITICAL)
    from cli.keys import preflight_or_onboard
    if not preflight_or_onboard(interactive=False):
        sys.exit(1)
    container = await build_cli_container(log_level="ERROR")
    _logging.disable(_logging.NOTSET)

    task_agent = container.get_agent("task_agent")
    if not task_agent:
        click.echo("TaskAgent not available")
        sys.exit(1)

    # Try to get breakdown from usage tracker
    orchestrator = None
    try:
        orchestrator = task_agent.get_orchestrator(session_id)
    except Exception:
        pass

    costs = {"session_id": session_id, "breakdown": "unavailable"}

    if orchestrator and hasattr(orchestrator, "usage_tracker"):
        try:
            breakdown = await orchestrator.usage_tracker.get_session_breakdown(session_id)
            costs["breakdown"] = breakdown
        except Exception:
            pass

    # Fallback: read llm_usage files
    if costs["breakdown"] == "unavailable":
        from agents.task.path import pm
        data_root = pm().data_root
        session_dir = None
        patterns = [
            f"*/{session_id}*/",
            f"*/sessions/{session_id}*/",
            f"*/*{session_id}*/",
        ]
        for pattern in patterns:
            for path in data_root.glob(pattern):
                if path.is_dir():
                    session_dir = path
                    break
            if session_dir:
                break

        if session_dir:
            usage = _summarize_llm_usage(session_dir)
            if usage:
                costs["llm_usage"] = usage

    if as_json:
        click.echo(json.dumps(costs, indent=2, default=str))
    else:
        click.echo(f"Costs for session {session_id[:16]}:")
        if isinstance(costs["breakdown"], dict):
            total = costs["breakdown"].get("total_credits", 0)
            click.echo(f"  Total credits: {total}")
        elif costs.get("llm_usage"):
            u = costs["llm_usage"]
            click.echo(f"  On-disk usage: {u['records']} call(s), "
                       f"{u['total_tokens']} tokens, ~${u['total_cost_estimate']} est.")
        else:
            click.echo(f"  {costs['breakdown']}")


@session.command("tools")
@click.argument("session_id")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def session_tools(session_id: str, as_json: bool):
    """Show tools used in a session."""
    from agents.task.path import pm

    data_root = pm().data_root
    session_dir = None
    patterns = [
        f"*/{session_id}*/",
        f"*/sessions/{session_id}*/",
        f"*/*{session_id}*/",
    ]
    for pattern in patterns:
        for path in data_root.glob(pattern):
            if path.is_dir():
                session_dir = path
                break  # inside the is_dir guard (was mis-indented; cf. session_artifacts)
        if session_dir:
            break

    if not session_dir:
        click.echo(click.style("[polyrob] ", fg="red") + f"Session directory not found for {session_id}")
        sys.exit(1)

    # Scan feed for tool calls
    feed_dir = session_dir / "feed"
    tool_calls = {}
    if feed_dir.exists():
        for feed_file in sorted(feed_dir.glob("*.json")):
            try:
                data = json.loads(feed_file.read_text())
                if data.get("type") == "tool_call":
                    name = data.get("name", "unknown")
                    tool_calls[name] = tool_calls.get(name, 0) + 1
            except (json.JSONDecodeError, OSError):
                pass

    if as_json:
        click.echo(json.dumps(tool_calls, indent=2))
    else:
        click.echo(f"Tools used in session {session_id[:16]}:")
        if tool_calls:
            for name, count in sorted(tool_calls.items(), key=lambda x: -x[1]):
                click.echo(f"  {name}: {count}")
        else:
            click.echo("  (no tool calls found)")
