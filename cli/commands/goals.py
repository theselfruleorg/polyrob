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
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()


def _warn_if_goals_off() -> None:
    """026 P0.4: a stored goal only runs if the dispatcher loop is on."""
    from cli._flag_warn import warn_if_flag_off
    from core.config_policy import AutonomyConfig

    warn_if_flag_off(
        "GOALS_ENABLED",
        "the goal board is durable, but no dispatcher will pick goals up.",
        enabled_fn=AutonomyConfig.goals_enabled,
    )


def _owner_tenant(user: Optional[str] = None) -> str:
    """The tenant a goal view or write acts on — the ONE admin resolver.

    C14: the views read ``board.list(user_id=None)``, so on a multi-tenant box
    they showed every tenant's backlog, and on a deployed box the shell's own
    identity never matched the service's. ``admin_owner_principal`` adopts the
    deployment's declaration, exactly as ``admin_data_dir`` adopts its home.
    """
    if user:
        return user
    from core.admin_data_home import AmbiguousDataHome, admin_owner_principal
    try:
        return admin_owner_principal()
    except AmbiguousDataHome as exc:
        raise click.ClickException(str(exc))


def _get_board(data_root: Optional[Path] = None) -> GoalBoard:
    """Get the GoalBoard instance for the current user."""
    from cli._admin_home import admin_data_dir
    from core.bootstrap import setup_project_path, setup_sqlite_compat

    setup_project_path()
    setup_sqlite_compat()

    if data_root is None:
        data_root = Path(admin_data_dir())

    # WS-3: one shared {data_dir}/goals.db resolver; the home is the admin
    # seam every owner verb uses (cli/_admin_home.py) and only the join is shared.
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
        STATUS_CANCELLED: "bright_black",
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
@click.option("--user", default=None, help="Tenant id (default: this instance's owner).")
@click.option("-n", "limit", type=int, default=30, show_default=True,
              help="How many of the newest goals to show (1-500).")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def goals_list(status: Optional[str], user: Optional[str], limit: int, as_json: bool):
    """What is on the board, newest first, with the counts over EVERY row."""
    # C14/E1: this called `board.list(user_id=None)` — the DISPATCHER's order
    # (`priority DESC, created_at ASC LIMIT 100`) used as a VIEW. On the 409-row
    # prod board that window was the OLDEST 100 rows and held none of the live
    # legs, so the seat told its owner there was no trading work while ten
    # cycles had run. `list_recent` is the view; `status_counts` is the whole
    # board, so the header can never be a window's arithmetic.
    board = _get_board()
    tenant = _owner_tenant(user)
    want = max(1, min(500, int(limit)))
    statuses = (status,) if status else None
    rows = board.list_recent(user_id=tenant, statuses=statuses, limit=want + 1)
    more = max(0, len(rows) - want)
    rows = rows[:want]
    try:
        counts = board.status_counts(user_id=tenant)
    except Exception as exc:                      # never a silent zero
        counts, counts_err = {}, f"{type(exc).__name__}: {exc}"
    else:
        counts_err = None
    _warn_if_goals_off()

    if as_json:
        click.echo(json.dumps({"user_id": tenant, "goals": [_goal_to_dict(g) for g in rows],
                               "more": more, "counts": counts,
                               "counts_error": counts_err}, indent=2))
        return

    if counts_err:
        click.echo(click.style(
            f"board counts unavailable ({counts_err}) — the list below is a "
            f"window, and how much it leaves out is UNKNOWN.", fg="yellow"))
    else:
        summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        click.echo(click.style(
            f"board — tenant {tenant}: {summary or 'no goals'}", dim=True))

    if not rows:
        from cli.ui.candy import empty
        click.echo(empty("goals" + (f" with status {status}" if status else ""),
                         "nothing is on this tenant's board"))
        return

    for g in rows:
        click.echo(_format_goal(g))
        click.echo()
    if more:
        click.echo(click.style(f"({more} more not shown — -n to raise the window)",
                               dim=True))


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
    board = _get_board()
    status = STATUS_TRIAGE if triage else STATUS_READY

    payload = {}
    if tools:
        payload["tools"] = [t.strip() for t in tools.split(",") if t.strip()]
    if acceptance:
        payload["acceptance"] = acceptance

    try:
        goal = board.create(
            user_id=_owner_tenant(),
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
    _warn_if_goals_off()


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

    success = board.update_status(goal_id, STATUS_READY, user_id=_owner_tenant())
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

    success = board.update_status(goal_id, STATUS_BLOCKED, user_id=_owner_tenant())
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

    success = board.update_status(goal_id, STATUS_READY, user_id=_owner_tenant())
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
    success = board.update_status(goal_id, STATUS_READY, reset_failures=True,
                                  user_id=_owner_tenant())
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
@click.option("--success-criteria", default=None,
              help="What the planner measures against (shown in its prompt).")
@click.option("--goal-budget", type=int, default=None,
              help="Max LIVE child goals before creates are refused (0 = no cap).")
@click.option("--stream-id", default=None,
              help="Ties this objective to a data/streams/streams.yaml entry.")
@click.option("--force", is_flag=True, help="Bypass near-duplicate rejection.")
def objective_add(title, body, priority, success_criteria, goal_budget, stream_id, force):
    board = _get_board()
    payload = {}
    if success_criteria:
        payload["success_criteria"] = success_criteria
    if goal_budget is not None:
        payload["goal_budget"] = int(goal_budget)
    if stream_id:
        payload["stream_id"] = stream_id
    try:
        o = board.create_objective(user_id=_owner_tenant(), title=title, body=body,
                                   priority=priority, force=force,
                                   payload=payload or None)
    except DuplicateGoalError as e:
        click.echo(click.style("[polyrob] ERROR: ", fg="red")
                   + f"near-duplicate of {e.match_id} '{e.match_title}' "
                     f"(similarity {e.similarity:.2f}); use --force to override")
        sys.exit(1)
    click.echo(click.style("[polyrob] ", fg="green") + f"Created objective {o.id} [active]: {o.title}")


@objective.command("show")
@click.argument("objective_id")
def objective_show(objective_id):
    """Show one objective: criteria, budget use, and its live children."""
    board = _get_board()
    user_id = _owner_tenant()
    o = board.get(objective_id)
    # GoalBoard.get() has no tenant filter (it is a plain SELECT by id), so a
    # known-id lookup must reject another tenant's row here rather than leak
    # its title/body/payload. Same idiom as GoalBoard.update_fields: fetch via
    # get(), then check user_id. Same message/exit as "not found" — a
    # different one would itself be a cross-tenant existence oracle.
    if o is None or o.kind != "objective" or o.user_id != user_id:
        click.echo(click.style("[polyrob] ERROR: ", fg="red") + "no such objective")
        sys.exit(1)
    payload = o.payload or {}
    click.echo(f"{o.id} [{o.status}] {o.title}")
    if o.body:
        click.echo(o.body)
    if payload.get("stream_id"):
        click.echo(f"stream: {payload['stream_id']}")
    if payload.get("success_criteria"):
        click.echo(f"success criteria: {payload['success_criteria']}")
    children = board.children_of(user_id, o.id)
    # children_of drops only cancelled/dropped, so `done` children are IN this
    # list — which is correct for the budget (a lifetime tally) and wrong to call
    # "live". Report the two numbers separately rather than one mislabelled one.
    in_flight = [c for c in children if c.status != "done"]
    budget = board.objective_budget(o)
    if budget > 0:
        click.echo(f"goal budget: {len(children)}/{budget} spent "
                   f"({len(in_flight)} in flight, done children count)")
    elif payload.get("stream_id"):
        click.echo(f"goal budget: none — stream objectives are uncapped "
                   f"({len(in_flight)} goal(s) in flight)")
    else:
        click.echo(f"goal budget: no cap ({len(in_flight)} goal(s) in flight, "
                   f"{len(children)} incl. done)")
    for c in children:
        click.echo(f"  {c.id[:8]} [{c.status}] {c.title}")


@objective.command("list")
@click.option("--status", type=click.Choice(["active", "paused", "done", "dropped"]))
def objective_list(status):
    board = _get_board()
    user_id = _owner_tenant()
    objs = board.objectives(user_id=user_id, status=status)
    if not objs:
        click.echo("No objectives.")
        return
    for o in objs:
        kids = board.children(o.id, user_id=user_id)
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
        ok = _get_board().set_objective_status(objective_id, target,
                                               user_id=_owner_tenant())
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
@click.option("--title", help="Replace the one-line title.")
@click.option("--body", help="Replace the instructions the run is given.")
@click.option("--priority", type=int, help="1-10; higher is served first in a pass.")
@click.option("--tools", help="Comma-separated tool ids this goal may use. "
                              "⚠️ This is an OPERATOR GRANT: a money verb "
                              "listed here is reachable on an autonomous run.")
@click.option("--acceptance", help="What 'done' must prove (ids / paths / urls).")
def goals_edit(goal_id, title, body, priority, tools, acceptance):
    """Change a goal's title, body, priority, toolset or acceptance test.

    A terminal goal (done / cancelled) is never edited — re-create it instead.
    """
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
@click.option("--user", default=None, help="Tenant id (default: this instance's owner)")
def goals_tree(user: Optional[str]):
    """Objectives with their goals; orphan goals at the end.

    Reads ``objectives()`` + ``list_recent()`` (newest first), never
    ``board.list`` — the dispatcher's priority order showed the OLDEST rows
    and hid every recent leg (2026-08-29; pinned by the parity ratchet).
    """
    board = _get_board()
    tenant = _owner_tenant(user)
    _TREE_LIMIT = 500
    objectives = board.objectives(user_id=tenant)
    recent = board.list_recent(user_id=tenant, limit=_TREE_LIMIT)
    truncated = len(recent) >= _TREE_LIMIT
    by_parent = {}
    orphans = []
    for g in recent:
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

    if not objectives and not orphans:
        # A blank answer is the one thing a board view may never give: it reads
        # identically to "the command did nothing". The ONE empty grammar.
        from cli.ui.candy import empty
        click.echo(empty("objectives or goals",
                         f"nothing is on tenant {tenant}'s board"))
        return
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
        # The remedy must be reachable: `goals list` is capped at 500 too, so
        # "the full set" was a promise no verb here keeps. Narrowing IS the
        # honest answer, and `status_counts` (printed by `goals list`) is the
        # arithmetic over EVERY row.
        click.echo(click.style(
            f"(the newest {_TREE_LIMIT} rows — older goals are not shown; narrow "
            f"with `polyrob goals list --status <status>`, whose header counts "
            f"every row on the board)",
            fg="yellow"))
