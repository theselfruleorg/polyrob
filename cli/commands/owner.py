"""polyrob owner — quick access to who can talk to / command the agent.

One place to see the bound owner + per-surface access posture, list the third-party
correspondents the agent is talking to, and approve a pending one. This is the single
admin seam for the WS-A three-tier access model (owner can command; correspondents are
DATA-only; unknown senders are denied).
"""
import os

import click


def _registry(data_dir: str):
    from core.surfaces.correspondents import CorrespondentRegistry
    return CorrespondentRegistry(os.path.join(data_dir, "correspondents.db"))


def _allowlist(data_dir: str):
    from core.surfaces.outbound_allowlist import OutboundAllowlist
    return OutboundAllowlist(os.path.join(data_dir, "surfaces.db"))


def _do_allow(allowlist, user_id, surface, target, note=""):
    """Pure handler (unit-testable without click): grant SURFACE:TARGET for USER_ID."""
    allowlist.allow(user_id, surface, target, note=note)
    return True


def _do_deny(allowlist, user_id, surface, target):
    """Pure handler: revoke SURFACE:TARGET for USER_ID. True if an active row was revoked."""
    return allowlist.revoke(user_id, surface, target)


def _do_allowlist(allowlist, user_id):
    """Pure handler: list allowlist rows for USER_ID."""
    return allowlist.list(user_id)


def _data_dir() -> str:
    """Resolve the SAME data home the surface daemons use (build_cli_container's
    _resolve_cli_data_home = <cwd>/.polyrob by default, or POLYROB_DATA_DIR).

    The old `POLYROB_DATA_DIR or "data"` pointed owner admin at ./data while the
    daemon wrote to <cwd>/.polyrob/correspondents.db — so `owner correspondents/
    approve/invite` silently operated on a DB the surface never read.
    """
    from core.bootstrap import _resolve_cli_data_home
    data_home, _, _ = _resolve_cli_data_home()
    return str(data_home)


@click.group()
def owner():
    """Inspect/manage who can command the agent and who it talks to."""
    # Bootstrap env BEFORE any subcommand reads config: file-set values written by
    # `polyrob config set …` (e.g. POLYROB_OWNER_USER_ID, CORRESPONDENT_ACCESS_ENABLED)
    # live in the .env layer, so without this `owner show` reports "unbound" and
    # `owner invite` can't honour a file-set access posture. Mirrors serve/kb/model
    # (order + local_mode=True).
    from core.bootstrap import load_env, setup_project_path, setup_sqlite_compat
    setup_project_path()
    setup_sqlite_compat()
    load_env(local_mode=True)


@owner.command("show")
def show():
    """Show the bound owner and per-surface access posture."""
    from core.surfaces.owner_admin import owner_access_summary
    s = owner_access_summary()
    op = s["owner_principal"] or click.style("(unbound — set POLYROB_OWNER_USER_ID)", fg="yellow")
    click.echo(click.style("owner: ", bold=True) + str(op))
    click.echo(f"correspondent access: {'on' if s['correspondent_access_enabled'] else 'off'}"
               f"  (require approval: {'yes' if s['require_approval'] else 'no'},"
               f" cap/day: {s['max_new_correspondents_per_day']})")
    click.echo(f"owner-by-email: {'on' if s['owner_by_email'] else 'off (v1: forgeable From:)'}")
    surf = ", ".join(f"{k}={'on' if v else 'off'}" for k, v in s["surfaces"].items())
    click.echo(f"surfaces: {surf}")


@owner.command("correspondents")
@click.option("--user", default=None, help="Filter to one tenant user_id")
def correspondents(user):
    """List the third-party correspondents the agent is talking to."""
    rows = _registry(_data_dir()).list(user_id=user)
    if not rows:
        click.echo(click.style("no correspondents", dim=True))
        return
    for r in rows:
        state = r["state"]
        color = {"active": "green", "pending": "yellow", "expired": "red"}.get(state, "white")
        click.echo(f"{click.style(state.ljust(8), fg=color)} "
                   f"{r['surface']}:{r['address']}  -> session {r['session_id']} "
                   f"(tenant {r['user_id']})")


@owner.command("invite")
@click.argument("surface")
@click.argument("address")
@click.argument("session_id")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
@click.option("--thread", default=None, help="Thread anchor (default: address-keyed)")
def invite(surface, address, session_id, user, thread):
    """Register a third party as a correspondent of SESSION_ID (owner-driven seed).

    Their replies then route to that session as DATA. Honours the approval gate +
    per-day cap (a pending invite needs `polyrob owner approve`).
    """
    import os
    os.environ.setdefault("CORRESPONDENT_ACCESS_ENABLED", "true")
    from core.instance import resolve_owner_principal
    from surfaces.email.seed import maybe_seed_correspondent

    tenant = user or resolve_owner_principal() or "local"

    class _C:
        def get_service(self, name):
            return _registry(_data_dir()) if name == "correspondent_registry" else None

    state = maybe_seed_correspondent(
        _C(), surface=surface, address=address, session_id=session_id,
        user_id=tenant, thread_id=thread, provenance="owner")
    color = {"active": "green", "pending": "yellow"}.get(state, "red")
    click.echo(click.style(f"invite {surface}:{address} -> session {session_id}: {state}",
                           fg=color)
               + ("  (run `polyrob owner approve` to activate)" if state == "pending" else ""))


def _instance_id() -> str:
    from core.instance import resolve_instance_id
    return resolve_instance_id()


def _owner_tenant(user) -> str:
    from core.instance import resolve_owner_principal
    return user or resolve_owner_principal() or "local"


def _allowlist_tenant(user) -> str:
    """Tenant resolution for the allow/deny/allowlist commands ONLY.

    These commands must write under the SAME tenant the `message` action reads at
    runtime — a local REPL session's user_id is `core.identity.resolve_identity()`
    (defaults to "local" when no owner is bound), NOT the instance id "rob" that
    `_owner_tenant` defaults to via `resolve_owner_principal(default_to_instance=True)`.
    Do not reuse `_owner_tenant` here; other owner commands intentionally keep that
    instance-id default.
    """
    from core.identity import resolve_identity
    return user or resolve_identity()


@owner.command("pending")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
def pending(user):
    """List the agent's PENDING self-evolution proposals (identity notes + skills).

    These are things the agent learned and quarantined for your review — they change
    nothing until you `owner promote` them.
    """
    from core import self_evolution
    tenant = _owner_tenant(user)
    items = self_evolution.list_pending(tenant, home_dir=_data_dir(),
                                        instance_id=_instance_id())
    if not items:
        click.echo(click.style("no pending proposals", dim=True))
        return
    click.echo(click.style(f"{len(items)} pending proposal(s) for tenant {tenant}:", bold=True))
    for it in items:
        label = "identity" if it["kind"] == "self_context" else "skill"
        click.echo(f"  {click.style(label.ljust(8), fg='yellow')} "
                   f"{click.style(it['kind'] + ':' + str(it['id']), bold=True)}"
                   f"  ({it['chars']} chars)")
        click.echo(f"           {it['preview']}")
    click.echo(click.style("\napprove: ", dim=True)
               + "polyrob owner promote <kind> <id>   "
               + click.style("reject: ", dim=True)
               + "polyrob owner reject <kind> <id>")


@owner.command("show-pending")
@click.argument("kind")
@click.argument("item_id")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
def show_pending(kind, item_id, user):
    """Show the FULL body of one pending proposal before deciding (T3-09).

    KIND is 'self_context' or 'skill'. `owner pending` shows only a ~160-char
    preview — review the whole quarantined body here, then promote/reject.
    """
    from core import self_evolution
    tenant = _owner_tenant(user)
    ok, body = self_evolution.show(kind, item_id, user_id=tenant,
                                   home_dir=_data_dir(), instance_id=_instance_id())
    if not ok:
        click.echo(click.style(body, fg="yellow"))
        raise SystemExit(1)
    click.echo(click.style(f"--- pending {kind}:{item_id} ---", bold=True))
    click.echo(body)


@owner.command("promote")
@click.argument("kind")
@click.argument("item_id")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
def promote(kind, item_id, user):
    """Promote a PENDING proposal to active. KIND is 'self_context' or 'skill'."""
    from core import self_evolution
    tenant = _owner_tenant(user)
    ok, msg = self_evolution.promote(kind, item_id, user_id=tenant,
                                     home_dir=_data_dir(), instance_id=_instance_id())
    click.echo(click.style(msg, fg="green" if ok else "yellow"))
    if not ok:
        raise SystemExit(1)


@owner.command("reject")
@click.argument("kind")
@click.argument("item_id")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
def reject(kind, item_id, user):
    """Reject (archive-then-discard) a PENDING proposal. KIND is 'self_context' or 'skill'."""
    from core import self_evolution
    tenant = _owner_tenant(user)
    ok, msg = self_evolution.reject(kind, item_id, user_id=tenant,
                                    home_dir=_data_dir(), instance_id=_instance_id())
    click.echo(click.style(msg, fg="green" if ok else "yellow"))
    if not ok:
        raise SystemExit(1)


def _goal_board():
    from pathlib import Path
    from agents.task.goals.board import GoalBoard
    from core.runtime_config import get_data_root
    return GoalBoard(str(Path(get_data_root()) / "goals.db"))


@owner.command("asks")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
def asks(user):
    """List the agent's OPEN asks — concrete needs blocking its progress (§7.2b).

    Fulfil one with `polyrob owner fulfill <id>` after providing what it asks for;
    that flips its blocked goals back to ready so work resumes.
    """
    from agents.task.goals.board import ASK_OPEN
    tenant = _owner_tenant(user)
    rows = _goal_board().asks(user_id=tenant, status=ASK_OPEN)
    if not rows:
        click.echo(click.style("no open asks", dim=True))
        return
    click.echo(click.style(f"{len(rows)} open ask(s) for tenant {tenant}:", bold=True))
    for a in rows:
        blocks = (a.payload or {}).get("blocks_goal_ids", [])
        click.echo(f"  {click.style(a.id, fg='cyan')}  {click.style(a.title, bold=True)}"
                   + (f"  (blocks {len(blocks)} goal(s))" if blocks else ""))
        if a.body:
            click.echo(f"           {a.body[:200]}")
    click.echo(click.style("\nfulfill: ", dim=True) + "polyrob owner fulfill <id>")


@owner.command("fulfill")
@click.argument("ask_id")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
def fulfill(ask_id, user):
    """Mark an ask FULFILLED and flip its blocked goals back to ready."""
    tenant = _owner_tenant(user)
    ok, unblocked = _goal_board().fulfill_ask(ask_id, user_id=tenant)
    if not ok:
        click.echo(click.style(f"no open ask '{ask_id}' for tenant {tenant}", fg="yellow"))
        raise SystemExit(1)
    click.echo(click.style(
        f"ask {ask_id} fulfilled — {unblocked} goal(s) unblocked", fg="green"))


@owner.command("approve")
@click.argument("surface")
@click.argument("address")
@click.option("--thread", default=None, help="Thread id (if the correspondent has several)")
@click.option("--user", default=None, help="Scope to one tenant user_id (multi-tenant safety)")
def approve(surface, address, thread, user):
    """Approve a PENDING correspondent so their replies route as DATA."""
    ok = _registry(_data_dir()).approve(surface=surface, address=address,
                                        thread_id=thread, user_id=user)
    if ok:
        click.echo(click.style(f"approved {surface}:{address}", fg="green"))
    else:
        click.echo(click.style(
            f"no pending correspondent {surface}:{address}"
            + (f" (thread {thread})" if thread else ""), fg="yellow"))


@owner.command("allow")
@click.argument("surface")
@click.argument("target")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
@click.option("--note", default="", help="Optional note (e.g. why this target is allowed)")
def allow(surface, target, user, note):
    """Allow the agent to send outbound messages to SURFACE:TARGET."""
    tenant = _allowlist_tenant(user)
    _do_allow(_allowlist(_data_dir()), tenant, surface, target, note=note)
    click.echo(click.style(f"allowed {surface}:{target} for tenant {tenant}", fg="green"))


@owner.command("deny")
@click.argument("surface")
@click.argument("target")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
def deny(surface, target, user):
    """Revoke outbound permission for SURFACE:TARGET."""
    tenant = _allowlist_tenant(user)
    ok = _do_deny(_allowlist(_data_dir()), tenant, surface, target)
    if ok:
        click.echo(click.style(f"denied {surface}:{target} for tenant {tenant}", fg="green"))
    else:
        click.echo(click.style(
            f"no active allowlist entry {surface}:{target} for tenant {tenant}", fg="yellow"))


@owner.command("allowlist")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
def allowlist(user):
    """List the outbound-send allowlist for a tenant."""
    tenant = _allowlist_tenant(user)
    rows = _do_allowlist(_allowlist(_data_dir()), tenant)
    if not rows:
        click.echo(click.style("no allowlist entries", dim=True))
        return
    for r in rows:
        color = "green" if r["status"] == "active" else "red"
        note = f"  ({r['note']})" if r["note"] else ""
        click.echo(f"{click.style(r['status'].ljust(8), fg=color)} "
                   f"{r['surface']}:{r['target']}{note}")


# --- Money loop: invoice admin ----------------------------------------------

def _bot_db_path():
    """Resolve the live bot.db (x402_payment_requests home): DB_PATH env wins,
    else the first existing candidate layout under the CLI data home."""
    db_path_env = os.getenv("DB_PATH")
    if db_path_env and os.path.isfile(db_path_env):
        return db_path_env
    from core.bootstrap import _resolve_cli_data_home
    from core.db_manifest import candidate_sqlite_dbs
    data_home, _, _ = _resolve_cli_data_home()
    for p in candidate_sqlite_dbs(data_home):
        if p.name == "bot.db" and p.is_file():
            return str(p)
    return None


async def _with_bot_db(coro_factory):
    """Open the bot DB, run coro_factory(db), close. Returns (ok, result_or_msg)."""
    from pathlib import Path
    path = _bot_db_path()
    if not path:
        return False, "no bot.db found (is this instance initialized?)"
    from modules.database.connection import DatabaseConnection
    db = DatabaseConnection(Path(path))
    await db.connect()
    try:
        return True, await coro_factory(db)
    finally:
        await db.close()


@owner.command("invoices")
@click.option("--user", default=None, help="Tenant user_id (default: all invoice rows)")
@click.option("--status", default=None, help="Filter: pending|completed|expired")
def invoices(user, status):
    """List agent-created x402 payment requests (invoices)."""
    import asyncio

    async def run(db):
        from modules.x402.invoicing import list_payment_requests
        if user:
            return await list_payment_requests(user_id=user, status=status, db=db)
        rows = await db.fetch_all(
            "SELECT * FROM x402_payment_requests WHERE metadata LIKE ? "
            "ORDER BY created_at DESC LIMIT 50",
            ('%"kind": "agent_invoice"%',))
        import json as _json
        out = []
        for r in rows or []:
            try:
                meta = _json.loads(r.get("metadata") or "{}")
            except Exception:
                meta = {}
            if status and r.get("status") != status:
                continue
            out.append({"request_id": r["id"], "amount_usd": r.get("amount_usd"),
                        "status": r.get("status"), "purpose": meta.get("purpose"),
                        "created_at": r.get("created_at")})
        return out

    ok, rows = asyncio.run(_with_bot_db(run))
    if not ok:
        click.echo(click.style(str(rows), fg="yellow"))
        return
    if not rows:
        click.echo(click.style("no invoices", dim=True))
        return
    for r in rows:
        color = {"pending": "yellow", "completed": "green", "expired": "red"}.get(r["status"], "white")
        click.echo(f"{click.style(str(r['status']).ljust(10), fg=color)} "
                   f"{r['request_id']}  ${float(r['amount_usd'] or 0):.2f}  "
                   f"{r.get('purpose') or '(no purpose)'}  ({r.get('created_at')})")


@owner.command("settle")
@click.argument("request_id")
@click.option("--tx-hash", default=None, help="On-chain tx hash, if any")
def settle(request_id, tx_hash):
    """Attest an invoice as PAID (pending -> completed). The settlement watcher
    then wakes the originating session and emits payment_settled."""
    import asyncio

    async def run(db):
        from modules.x402.invoicing import settle_payment_request
        return await settle_payment_request(request_id, transaction_hash=tx_hash, db=db)

    ok, settled = asyncio.run(_with_bot_db(run))
    if not ok:
        click.echo(click.style(str(settled), fg="yellow"))
    elif settled:
        click.echo(click.style(f"settled {request_id}", fg="green"))
    else:
        click.echo(click.style(
            f"{request_id} not settled (unknown id or not pending)", fg="yellow"))
