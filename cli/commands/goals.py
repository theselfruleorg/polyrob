"""POLYROB goals commands — manage the durable goal board (W4).

The goal board is a cross-session backlog of agent-pursued goals. Goals outlive
the turn that created them and are claimed/run by the dispatcher.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import click

from agents.task.goals.board import (
    KIND_GOAL,
    KIND_OBJECTIVE,
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_READY,
    STATUS_RUNNING,
    STATUS_TRIAGE,
    DuplicateGoalError,
    Goal,
    GoalBoard,
)


@click.group("goals")
def goals():
    """Manage durable goals board."""
    pass


def _get_board(data_root: Optional[Path] = None) -> GoalBoard:
    """Get the GoalBoard instance for the current user."""
    from core.bootstrap import setup_project_path, setup_sqlite_compat
    from core.runtime_config import get_data_root

    setup_project_path()
    setup_sqlite_compat()

    if data_root is None:
        data_root = Path(get_data_root())

    # WS-3: one shared {data_dir}/goals.db resolver. The CLI keeps its own
    # get_data_root() home resolution (parity-pinned against the dispatcher by
    # tests/unit/core/test_cli_data_home_isolation.py) and only the join is shared.
    from core.runtime_paths import goals_db_path
    return GoalBoard(goals_db_path(str(data_root)))


def _format_goal(goal: Goal) -> str:
    """Format a goal for display."""
    created = datetime.fromtimestamp(goal.created_at).strftime("%Y-%m-%d %H:%M")
    status_color = {
        STATUS_READY: "green",
        STATUS_RUNNING: "yellow",
        STATUS_DONE: "blue",
        STATUS_BLOCKED: "red",
        STATUS_CANCELLED: "dim",
        STATUS_TRIAGE: "cyan",
    }.get(goal.status, "white")

    lines = [
        f"{click.style(goal.id, fg='cyan')}: {click.style(goal.title, bold=True)}",
        f"  status: {click.style(goal.status, fg=status_color)}",
        f"  created: {created}",
    ]
    if goal.kind == KIND_OBJECTIVE:
        lines.append(f"  kind: {goal.kind}")
    if goal.body:
        lines.append(f"  body: {goal.body}")
    if goal.priority != 5:
        lines.append(f"  priority: {goal.priority}")
    if goal.parent_id:
        lines.append(f"  parent: {goal.parent_id}")
    if goal.session_id:
        lines.append(f"  session: {goal.session_id}")
    if goal.consecutive_failures > 0:
        lines.append(f"  failures: {goal.consecutive_failures}/{goal.max_retries}")
    if goal.last_failure_error:
        lines.append(f"  last error: {goal.last_failure_error}")
    if goal.result:
        lines.append(f"  result: {goal.result}")
    if goal.status == STATUS_DONE:
        outcome = (goal.payload or {}).get("outcome")
        lines.append(f"  outcome: {outcome}" if outcome else "  [no outcome]")
    return "\n".join(lines)


def _goal_to_dict(goal: Goal) -> dict:
    """Convert a goal to a dict for JSON output."""
    return {
        "id": goal.id,
        "user_id": goal.user_id,
        "title": goal.title,
        "body": goal.body,
        "status": goal.status,
        "priority": goal.priority,
        "parent_id": goal.parent_id,
        "claim_lock": goal.claim_lock,
        "claim_expires": goal.claim_expires,
        "consecutive_failures": goal.consecutive_failures,
        "max_retries": goal.max_retries,
        "last_failure_error": goal.last_failure_error,
        "session_id": goal.session_id,
        "result": goal.result,
        "payload": goal.payload,
        "created_at": goal.created_at,
        "started_at": goal.started_at,
        "completed_at": goal.completed_at,
        "last_heartbeat_at": goal.last_heartbeat_at,
    }


@goals.command("list")
@click.option("--status", type=click.Choice(["ready", "running", "done", "blocked", "cancelled", "triage"]), help="Filter by status.")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def goals_list(status: Optional[str], as_json: bool):
    """List goals."""
    board = _get_board()
    goals_list = board.list(user_id=None, status=status)

    if as_json:
        click.echo(json.dumps([_goal_to_dict(g) for g in goals_list], indent=2))
        return

    if not goals_list:
        click.echo("No goals found.")
        return

    for g in goals_list:
        click.echo(_format_goal(g))
        click.echo()


@goals.command("show")
@click.argument("goal_id")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def goals_show(goal_id: str, as_json: bool):
    """Show details for a single goal."""
    board = _get_board()
    goal = board.get(goal_id)

    if goal is None:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + f"goal not found: {goal_id}")
        sys.exit(1)

    if as_json:
        click.echo(json.dumps(_goal_to_dict(goal), indent=2))
    else:
        click.echo(_format_goal(goal))


@goals.command("create")
@click.argument("title")
@click.option("--body", "-b", default="", help="Goal description / instructions.")
@click.option("--priority", "-p", type=int, default=5, help="Priority (1-10, default 5).")
@click.option("--parent", help="Parent goal ID (for sub-goals).")
@click.option("--triage", is_flag=True, help="Create in 'triage' status instead of 'ready'.")
@click.option("--tools", help="Comma-separated tool ids this goal may use.")
@click.option("--acceptance", help="What 'done' must prove (ids/paths/urls).")
@click.option("--objective", "objective_id", help="Parent objective id.")
@click.option("--force", is_flag=True, help="Bypass near-duplicate rejection.")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def goals_create(title: str, body: str, priority: int, parent: Optional[str], triage: bool,
                  tools: Optional[str], acceptance: Optional[str], objective_id: Optional[str],
                  force: bool, as_json: bool):
    """Create a new goal."""
    from core.identity import resolve_identity

    board = _get_board()
    status = STATUS_TRIAGE if triage else STATUS_READY

    payload = {}
    if tools:
        payload["tools"] = [t.strip() for t in tools.split(",") if t.strip()]
    if acceptance:
        payload["acceptance"] = acceptance

    try:
        goal = board.create(
            user_id=resolve_identity(),
            title=title,
            body=body,
            priority=priority,
            parent_id=objective_id or parent,
            status=status,
            payload=payload or None,
            force=force,
        )
    except DuplicateGoalError as e:
        click.echo(click.style("[polyrob] ERROR: ", fg="red")
                   + f"duplicate of {e.match_id} '{e.match_title}' "
                     f"(similarity {e.similarity:.2f}); use --force to override")
        sys.exit(1)
    except ValueError as e:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + str(e))
        sys.exit(1)

    if as_json:
        click.echo(json.dumps(_goal_to_dict(goal), indent=2))
    else:
        click.echo(click.style("[polyrob] ", fg="green") + f"Created goal {goal.id}")
        click.echo(_format_goal(goal))


@goals.command("ready")
@click.argument("goal_id")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def goals_ready(goal_id: str, as_json: bool):
    """Mark a triage/blocked goal as ready."""
    board = _get_board()
    goal = board.get(goal_id)

    if goal is None:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + f"goal not found: {goal_id}")
        sys.exit(1)

    if goal.status not in (STATUS_TRIAGE, STATUS_BLOCKED):
        click.echo(click.style("[polyrob] ERROR: ", fg="red") +
                   f"goal is {goal.status}, only triage/blocked goals can be marked ready")
        sys.exit(1)

    success = board.update_status(goal_id, STATUS_READY)
    if success:
        if as_json:
            updated = board.get(goal_id)
            click.echo(json.dumps(_goal_to_dict(updated), indent=2))
        else:
            click.echo(click.style("[polyrob] ", fg="green") + f"Goal {goal_id} marked as ready")
    else:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + f"failed to update goal")
        sys.exit(1)


@goals.command("pause")
@click.argument("goal_id")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def goals_pause(goal_id: str, as_json: bool):
    """Pause a goal (move to blocked status)."""
    board = _get_board()
    goal = board.get(goal_id)

    if goal is None:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + f"goal not found: {goal_id}")
        sys.exit(1)

    if goal.status == STATUS_RUNNING:
        click.echo(click.style("[polyrob] WARNING: ", fg="yellow") +
                   "goal is currently running — pause may not take effect immediately")

    success = board.update_status(goal_id, STATUS_BLOCKED)
    if success:
        if as_json:
            updated = board.get(goal_id)
            click.echo(json.dumps(_goal_to_dict(updated), indent=2))
        else:
            click.echo(click.style("[polyrob] ", fg="green") + f"Goal {goal_id} paused (blocked)")
    else:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + f"failed to update goal")
        sys.exit(1)


@goals.command("resume")
@click.argument("goal_id")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def goals_resume(goal_id: str, as_json: bool):
    """Resume a paused/blocked goal."""
    board = _get_board()
    goal = board.get(goal_id)

    if goal is None:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + f"goal not found: {goal_id}")
        sys.exit(1)

    if goal.status != STATUS_BLOCKED:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") +
                   f"goal is {goal.status}, only blocked goals can be resumed")
        sys.exit(1)

    success = board.update_status(goal_id, STATUS_READY)
    if success:
        if as_json:
            updated = board.get(goal_id)
            click.echo(json.dumps(_goal_to_dict(updated), indent=2))
        else:
            click.echo(click.style("[polyrob] ", fg="green") + f"Goal {goal_id} resumed")
    else:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + f"failed to update goal")
        sys.exit(1)


@goals.command("cancel")
@click.argument("goal_id")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def goals_cancel(goal_id: str, as_json: bool):
    """Cancel a goal."""
    board = _get_board()
    goal = board.get(goal_id)

    if goal is None:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + f"goal not found: {goal_id}")
        sys.exit(1)

    # A goal already in a terminal state can't be cancelled (board.cancel excludes
    # done/cancelled) — that's a clean no-op, NOT a "failed to cancel" error+exit 1.
    if goal.status in (STATUS_DONE, "cancelled"):
        if as_json:
            click.echo(json.dumps(_goal_to_dict(goal), indent=2))
        else:
            click.echo(click.style("[polyrob] ", fg="yellow")
                       + f"goal {goal_id} is already {goal.status} — nothing to cancel")
        return

    success = board.cancel(goal_id)
    if success:
        if as_json:
            updated = board.get(goal_id)
            click.echo(json.dumps(_goal_to_dict(updated), indent=2))
        else:
            click.echo(click.style("[polyrob] ", fg="green") + f"Goal {goal_id} cancelled")
    else:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + f"failed to cancel goal")
        sys.exit(1)


@goals.command("retry")
@click.argument("goal_id")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def goals_retry(goal_id: str, as_json: bool):
    """Retry a blocked/failed goal (resets failures)."""
    board = _get_board()
    goal = board.get(goal_id)

    if goal is None:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + f"goal not found: {goal_id}")
        sys.exit(1)

    if goal.status not in (STATUS_BLOCKED,):
        click.echo(click.style("[polyrob] ERROR: ", fg="red") +
                   f"goal is {goal.status}, only blocked goals can be retried")
        sys.exit(1)

    # Reset to ready and clear failures
    success = board.update_status(goal_id, STATUS_READY, reset_failures=True)
    if success:
        if as_json:
            updated = board.get(goal_id)
            click.echo(json.dumps(_goal_to_dict(updated), indent=2))
        else:
            click.echo(click.style("[polyrob] ", fg="green") +
                       f"Goal {goal_id} reset to ready (failures cleared)")
    else:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + f"failed to retry goal")
        sys.exit(1)


@goals.command("events")
@click.argument("goal_id")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def goals_events(goal_id: str, as_json: bool):
    """Show event timeline for a goal."""
    board = _get_board()
    events = board.events(goal_id)

    if not events:
        click.echo(f"No events found for goal {goal_id}")
        return

    if as_json:
        click.echo(json.dumps(events, indent=2))
        return

    click.echo(f"Events for goal {goal_id}:")
    click.echo("-" * 60)
    for e in events:
        ts = datetime.fromtimestamp(e["created_at"]).strftime("%Y-%m-%d %H:%M:%S")
        payload = e.get("payload") or {}  # board.events() already json-decodes payload
        payload_str = " ".join(f"{k}={v}" for k, v in payload.items()) if payload else ""
        click.echo(f"  {ts}  {e['kind']:<12} {payload_str}")


@goals.group("objective")
def objective():
    """Manage standing objectives (the durable 'why' behind goals)."""


@objective.command("add")
@click.argument("title")
@click.option("--body", "-b", default="", help="What success looks like; constraints.")
@click.option("--priority", "-p", type=int, default=5)
@click.option("--force", is_flag=True, help="Bypass near-duplicate rejection.")
def objective_add(title, body, priority, force):
    from core.identity import resolve_identity
    board = _get_board()
    try:
        o = board.create_objective(user_id=resolve_identity(), title=title, body=body,
                                   priority=priority, force=force)
    except DuplicateGoalError as e:
        click.echo(click.style("[polyrob] ERROR: ", fg="red")
                   + f"near-duplicate of {e.match_id} '{e.match_title}' "
                     f"(similarity {e.similarity:.2f}); use --force to override")
        sys.exit(1)
    click.echo(click.style("[polyrob] ", fg="green") + f"Created objective {o.id} [active]: {o.title}")


@objective.command("list")
@click.option("--status", type=click.Choice(["active", "paused", "done", "dropped"]))
def objective_list(status):
    from core.identity import resolve_identity
    board = _get_board()
    objs = board.objectives(user_id=resolve_identity(), status=status)
    if not objs:
        click.echo("No objectives.")
        return
    for o in objs:
        kids = board.children(o.id)
        counts = {}
        for k in kids:
            counts[k.status] = counts.get(k.status, 0) + 1
        summary = ", ".join(f"{v} {s}" for s, v in sorted(counts.items())) or "no goals yet"
        click.echo(f"{click.style(o.id, fg='cyan')} [{o.status}] p{o.priority}: "
                   f"{click.style(o.title, bold=True)} ({summary})")
        if o.body:
            click.echo(f"  {o.body}")


def _objective_status_cmd(name, target):
    @objective.command(name)
    @click.argument("objective_id")
    def _cmd(objective_id):
        from core.identity import resolve_identity
        ok = _get_board().set_objective_status(objective_id, target,
                                               user_id=resolve_identity())
        if ok:
            click.echo(click.style("[polyrob] ", fg="green") + f"Objective {objective_id} -> {target}")
        else:
            click.echo(click.style("[polyrob] ERROR: ", fg="red") + "no such objective")
            sys.exit(1)
    return _cmd


_objective_status_cmd("pause", "paused")
_objective_status_cmd("activate", "active")
_objective_status_cmd("drop", "dropped")


@goals.command("edit")
@click.argument("goal_id")
@click.option("--title")
@click.option("--body")
@click.option("--priority", type=int)
@click.option("--tools", help="Comma-separated tool ids.")
@click.option("--acceptance", help="What 'done' must prove.")
def goals_edit(goal_id, title, body, priority, tools, acceptance):
    board = _get_board()
    patch = {}
    if acceptance is not None:
        patch["acceptance"] = acceptance
    if tools is not None:
        patch["tools"] = [t.strip() for t in tools.split(",") if t.strip()]
    ok = board.update_fields(goal_id, title=title, body=body, priority=priority,
                             payload_patch=patch or None)
    if ok:
        click.echo(click.style("[polyrob] ", fg="green") + f"Updated goal {goal_id}")
        click.echo(_format_goal(board.get(goal_id)))
    else:
        click.echo(click.style("[polyrob] ERROR: ", fg="red")
                   + "goal not found, terminal, or nothing to change")
        sys.exit(1)


@goals.command("tree")
def goals_tree():
    """Objectives with their goals; orphan goals at the end."""
    board = _get_board()
    _TREE_LIMIT = 500
    everything = board.list(limit=_TREE_LIMIT)
    truncated = len(everything) >= _TREE_LIMIT
    objectives = [g for g in everything if g.kind == KIND_OBJECTIVE]
    by_parent = {}
    orphans = []
    for g in everything:
        if g.kind != KIND_GOAL:
            continue
        if g.parent_id:
            by_parent.setdefault(g.parent_id, []).append(g)
        else:
            orphans.append(g)

    def _leaf(g):
        outcome = (g.payload or {}).get("outcome")
        note = f"  outcome: {outcome}" if outcome else ("  [no outcome]" if g.status == "done" else "")
        click.echo(f"  - {click.style(g.id, fg='cyan')} [{g.status}] {g.title}{note}")

    for o in objectives:
        click.echo(f"{click.style(o.id, fg='cyan')} [{o.status}] "
                   f"{click.style(o.title, bold=True)}")
        for g in by_parent.get(o.id, []):
            _leaf(g)
    if orphans:
        click.echo(click.style("(no objective)", dim=True))
        for g in orphans:
            _leaf(g)
    if truncated:
        click.echo(click.style(
            f"(first {_TREE_LIMIT} rows — truncated; use `goals list --json` for the full set)",
            fg="yellow"))
