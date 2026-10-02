"""``polyrob workers`` — the owner seat for named workers (041 phase 2).

A worker is a reusable, named agent configuration the orchestrator dispatches
with ``delegate_task(profile="<id>")``: a description (WHEN to use it), a
standing brief, an optional tool list (never wider than the parent's), an
optional pinned model and step budget. Files live under the deployed data home
(``<home>/profiles/user_{uid}/``) and are written through
``agents.task.agent.profile_store.ProfileStore`` — the same scan / quarantine /
archive-never-delete lane the agent's ``worker_manage`` proposal rides.

The OWNER seat is the authority: a worker written here is approved (unless the
threat scan flags it — then it waits in ``.pending/`` like any other), and
``approve`` moves an agent-proposed draft into the active lane.

``list`` / ``show`` are READS (``admin_data_dir(write=False)``); every other
verb mutates the shared home. The REPL ``/workers`` renders through the SAME
helpers below, so the two seats cannot describe a worker differently.
"""
from __future__ import annotations

import json
from typing import List, Optional

import click


def _owner() -> str:
    from core.admin_data_home import AmbiguousDataHome, admin_owner_principal
    try:
        return admin_owner_principal()
    except AmbiguousDataHome as exc:
        raise click.ClickException(str(exc))


def _store(*, write: bool):
    from cli._admin_home import admin_data_dir
    from agents.task.agent.profile_store import ProfileStore
    return ProfileStore(home_dir=admin_data_dir(write=write))


# --- shared renderers (the CLI and the REPL both call these) ---------------------

def _spec_line(spec) -> str:
    tools = ", ".join(spec.tool_ids) if spec.tool_ids is not None else "inherits the parent's"
    who = "" if spec.owner_authored else "  [written by the agent]"
    return f"  {spec.id:<18} {spec.description or '(no description)'}\n" \
           f"  {'':<18} tools: {tools}{who}"


def render_list(store, user_id: str, *, enabled: Optional[bool] = None) -> str:
    """Approved + pending workers, one block. Honest when the store is unreadable."""
    from agents.task.agent.profile_store import WorkerSpec, workers_enabled
    enabled = workers_enabled() if enabled is None else enabled
    try:
        approved = store.list_approved(user_id)
        pending = store.list_pending(user_id)
    except Exception as exc:  # an unreadable store is not an empty one
        return f"Workers: unavailable({type(exc).__name__}: {exc})"
    lines: List[str] = []
    if not enabled:
        from core.remedy import flag_remedy
        lines.append("Workers are switched off on this instance — the agent cannot "
                     f"dispatch them. ({flag_remedy('WORKERS_ENABLED')})")
    lines.append(f"Approved ({len(approved)}):")
    lines += [_spec_line(WorkerSpec(m)) for m in approved] or \
        ["  — none yet. `polyrob workers new <id> --description …` adds one."]
    if pending:
        lines.append(f"Waiting for your approval ({len(pending)}):")
        lines += [f"  {pid}   → polyrob workers approve {pid}" for pid in pending]
    return "\n".join(lines)


def render_show(store, user_id: str, worker_id: str) -> str:
    from agents.task.agent.profile_store import WorkerSpec
    model = store.get_approved(worker_id, user_id=user_id)
    state = "approved"
    if model is None:
        model, state = store.get_pending(worker_id, user_id=user_id), "waiting for your approval"
    if model is None:
        return f"No worker '{worker_id}'."
    spec = WorkerSpec(model)
    out = [f"{spec.id} — {state}",
           f"  when to use: {spec.description or '(no description)'}",
           f"  tools:       {', '.join(spec.tool_ids) if spec.tool_ids is not None else 'inherits the parent’s'}",
           f"  model:       {spec.model or 'inherits the parent’s'}",
           f"  max steps:   {spec.max_steps or 'the delegation default'}",
           f"  written by:  {'you' if spec.owner_authored else 'the agent'}"]
    if spec.instructions:
        out.append("  brief:")
        out += [f"    {line}" for line in spec.instructions.splitlines()]
    return "\n".join(out)


def render_live(manager) -> str:
    """The live worker lines of ONE session's SubAgentManager (a read)."""
    rows = manager.live_workers() if manager is not None else []
    if not rows:
        return "No worker is running in this session."
    lines = [f"Running ({len(rows)}):"]
    for r in rows:
        lines.append(f"  {r['id']}  {r['worker']:<14} {int(r['elapsed'])}s  "
                     f"{(r['goal'] or '')[:60]}")
    lines.append("  /workers stop <id> · /workers steer <id> <text>")
    return "\n".join(lines)


def write_worker(store, user_id: str, worker_id: str, *, description: str,
                 instructions: str = "", tools: Optional[List[str]] = None,
                 model: str = "", provider: str = "", max_steps: Optional[int] = None) -> str:
    """Owner write (new / edit). Returns one honest line."""
    from agents.task.agent.profile_store import (PROVENANCE_USER, build_worker_profile,
                                                 is_valid_profile_id)
    if not is_valid_profile_id(worker_id):
        return f"'{worker_id}' is not a valid worker id (lowercase letters, digits, dashes)."
    data = build_worker_profile(worker_id, description=description, instructions=instructions,
                                tools=tools, model=model, provider=provider,
                                max_steps=max_steps)
    res = store.save_profile(data, user_id=user_id, created_by=PROVENANCE_USER)
    if not res.ok:
        return "Not saved: " + "; ".join(res.errors)
    if res.pending:
        return (f"Saved '{worker_id}', but the threat scan flagged it — it waits in "
                f"review (`polyrob workers show {worker_id}`, then approve).")
    return f"Saved '{worker_id}'. The agent sees it from its next session."


def approve_worker(store, user_id: str, worker_id: str) -> str:
    res = store.approve(worker_id, user_id=user_id)
    if not res.ok:
        return "Not approved: " + "; ".join(res.errors)
    return f"Approved '{worker_id}'. The agent can dispatch it from its next session."


def remove_worker(store, user_id: str, worker_id: str) -> str:
    if store.remove_profile(worker_id, user_id=user_id):
        return f"Removed '{worker_id}' (archived, recoverable)."
    return f"No worker '{worker_id}'."


# --- click ------------------------------------------------------------------------

@click.group("workers")
def workers():
    """Named workers: reusable helpers the agent dispatches by name."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()


@workers.command("list")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def workers_list(as_json: bool):
    """Approved workers and the ones waiting for your approval."""
    store, uid = _store(write=False), _owner()
    if as_json:
        from agents.task.agent.profile_store import WorkerSpec
        click.echo(json.dumps({
            "approved": [{"id": s.id, "description": s.description, "tools": s.tool_ids,
                          "model": s.model or None, "max_steps": s.max_steps,
                          "owner_authored": s.owner_authored}
                         for s in (WorkerSpec(m) for m in store.list_approved(uid))],
            "pending": store.list_pending(uid)}, indent=2))
        return
    click.echo(render_list(store, uid))


@workers.command("show")
@click.argument("worker_id")
def workers_show(worker_id: str):
    """One worker, approved or pending."""
    click.echo(render_show(_store(write=False), _owner(), worker_id))


def _write_options(f):
    f = click.option("--max-steps", type=click.IntRange(1, 50), default=None,
                     help="Pin the step budget.")(f)
    f = click.option("--provider", default="", help="Provider for --model.")(f)
    f = click.option("--model", default="", help="Pin a model (else the parent's).")(f)
    f = click.option("--tools", default=None,
                     help="Comma-separated tool ids (else the parent's set).")(f)
    f = click.option("--instructions", default="", help="The worker's standing brief.")(f)
    return f


def _tools(raw: Optional[str]) -> Optional[List[str]]:
    return None if raw is None else [t.strip() for t in raw.split(",") if t.strip()]


@workers.command("new")
@click.argument("worker_id")
@click.option("--description", required=True, help="WHEN the agent should use it.")
@_write_options
def workers_new(worker_id, description, instructions, tools, model, provider, max_steps):
    """Create a worker (approved: you are the owner)."""
    store, uid = _store(write=True), _owner()
    if store.get_approved(worker_id, user_id=uid) is not None:
        raise click.ClickException(f"'{worker_id}' exists — use `polyrob workers edit`.")
    click.echo(write_worker(store, uid, worker_id, description=description,
                            instructions=instructions, tools=_tools(tools), model=model,
                            provider=provider, max_steps=max_steps))


@workers.command("edit")
@click.argument("worker_id")
@click.option("--description", default=None, help="WHEN the agent should use it.")
@_write_options
def workers_edit(worker_id, description, instructions, tools, model, provider, max_steps):
    """Change a worker; unset options keep their current value."""
    from agents.task.agent.profile_store import WorkerSpec
    store, uid = _store(write=True), _owner()
    model_row = store.get_approved(worker_id, user_id=uid) or store.get_pending(
        worker_id, user_id=uid)
    if model_row is None:
        raise click.ClickException(f"No worker '{worker_id}'.")
    cur = WorkerSpec(model_row)
    click.echo(write_worker(
        store, uid, worker_id,
        description=cur.description if description is None else description,
        instructions=instructions or cur.instructions,
        tools=cur.tool_ids if tools is None else _tools(tools),
        model=model or cur.model, provider=provider or cur.provider,
        max_steps=max_steps if max_steps is not None else cur.max_steps))


@workers.command("approve")
@click.argument("worker_id")
def workers_approve(worker_id: str):
    """Approve a worker the agent proposed."""
    click.echo(approve_worker(_store(write=True), _owner(), worker_id))


@workers.command("remove")
@click.argument("worker_id")
def workers_remove(worker_id: str):
    """Remove a worker (archived, never destroyed)."""
    click.echo(remove_worker(_store(write=True), _owner(), worker_id))
