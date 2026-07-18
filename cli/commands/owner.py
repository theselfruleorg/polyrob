"""polyrob owner — quick access to who can talk to / command the agent.

One place to see the bound owner + per-surface access posture, list the third-party
correspondents the agent is talking to, and approve a pending one. This is the single
admin seam for the WS-A three-tier access model (owner can command; correspondents are
DATA-only; unknown senders are denied).
"""
import logging
import os

import click

logger = logging.getLogger(__name__)


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
    """Resolve the SAME data home the surface daemons use — via the ONE core
    policy seam ``core.runtime_paths.resolve_data_home`` (POLYROB_DATA_DIR wins,
    else <cwd>/.polyrob). The old `POLYROB_DATA_DIR or "data"` pointed owner
    admin at ./data while the daemon wrote to <cwd>/.polyrob/correspondents.db —
    so `owner correspondents/approve/invite` silently operated on a DB the
    surface never read. Never re-implement resolution here.
    """
    from core.runtime_paths import resolve_data_home
    return str(resolve_data_home())


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
    from agents.task.constants import autonomy_mode_display
    click.echo(f"autonomy mode: {autonomy_mode_display()}")
    click.echo(f"correspondent access: {'on' if s['correspondent_access_enabled'] else 'off'}"
               f"  (require approval: {'yes' if s['require_approval'] else 'no'},"
               f" cap/day: {s['max_new_correspondents_per_day']})")
    click.echo(f"owner-by-email: {'on' if s['owner_by_email'] else 'off (v1: forgeable From:)'}")
    surf = ", ".join(f"{k}={'on' if v else 'off'}" for k, v in s["surfaces"].items())
    click.echo(f"surfaces: {surf}")


@owner.command("halt")
def halt_cmd():
    """Kill-switch: stop ALL autonomous dispatch + agent spend (no restart needed).

    Writes an AUTONOMY_HALT file to the resolved data home; every autonomous loop,
    trade, and payment refuses while it exists. Clear it with `polyrob owner resume`.
    """
    import os
    base = _data_dir()
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, "AUTONOMY_HALT")
    with open(path, "w") as fh:
        fh.write("halted by `polyrob owner halt`\n")
    click.echo(click.style("⛔ Autonomy HALTED", fg="red", bold=True)
               + f" — wrote {path}.")
    click.echo("No autonomous dispatch, trade, or agent spend will run until "
               "`polyrob owner resume`.")


@owner.command("resume")
def resume_cmd():
    """Clear the kill-switch set by `polyrob owner halt`."""
    import os
    path = os.path.join(_data_dir(), "AUTONOMY_HALT")
    if os.path.exists(path):
        os.remove(path)
        click.echo(click.style("✅ Autonomy RESUMED", fg="green", bold=True)
                   + f" — removed {path}.")
    else:
        click.echo("Autonomy was not halted (no halt file).")
    if (os.environ.get("AUTONOMY_HALT") or "").strip():
        click.echo(click.style(
            "⚠ AUTONOMY_HALT is still set in the environment — that ALSO halts. "
            "Unset it in your env file to fully resume.", fg="yellow"))


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
    from core.surfaces.seed import maybe_seed_correspondent

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


def _money_tenant(user) -> str:
    """M16 (2026-07-15): the ONE tenant resolver for the money listings
    (`owner sub`, and shared with `polyrob finance`).

    The agent's money rows (x402 invoices, subscriptions) are created under the
    runtime session's user_id = ``core.identity.resolve_identity()`` (owner-if-
    bound else "local") — the SAME resolver `polyrob finance` uses. `_owner_tenant`
    resolves to the instance id ("rob") when unbound, which reads a DIFFERENT
    bucket, so the sibling money views disagreed on an unbound install. Use THIS
    for money listings so finance and `owner sub` agree; print the scope so the
    owner always sees which tenant a listing is for.
    """
    from core.identity import resolve_identity
    return user or resolve_identity()


def _pending_correspondent_items(registry, tenant):
    """Pure handler (unit-testable without click): pending correspondent bindings
    for TENANT as owner-pending items (E5 — previously invisible outside
    `owner correspondents`, so replies silently DENIED until manual approval)."""
    items = []
    try:
        for r in registry.list(user_id=tenant):
            if r.get("state") != "pending":
                continue
            items.append({
                "kind": "correspondent",
                "id": f"{r['surface']}:{r['address']}",
                "chars": 0,
                "preview": (f"{r['surface']}:{r['address']} -> session "
                            f"{r['session_id']}  (approve: polyrob owner approve "
                            f"{r['surface']} {r['address']})"),
            })
    except Exception:
        # L10 (2026-07-15): was a silent `except: pass` — a broken correspondent
        # registry would hide pending contacts with no trace. Log it (the pending
        # listing still degrades gracefully to whatever was collected).
        logger.warning("owner pending: correspondent registry read failed", exc_info=True)
    return items


@owner.command("pending")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
def pending(user):
    """List the agent's PENDING self-evolution proposals (identity notes + skills),
    queued tool-approval requests (Task 9 / G-2 — PAYMENT_APPROVAL_MODE=approve),
    AND pending correspondent bindings (their replies are unroutable until approved).

    These are things the agent learned/asked and quarantined for your review — they
    change nothing until you `owner promote` (or reject) them.
    """
    from core import self_evolution
    from tools.controller.approval_queue import list_pending_tool_approvals
    tenant = _owner_tenant(user)
    items = self_evolution.list_pending(tenant, home_dir=_data_dir(),
                                        instance_id=_instance_id())
    items = items + list_pending_tool_approvals(_goal_board(), tenant)
    items = items + _pending_correspondent_items(_registry(_data_dir()), tenant)
    if not items:
        click.echo(click.style("no pending proposals", dim=True))
        return
    click.echo(click.style(f"{len(items)} pending proposal(s) for tenant {tenant}:", bold=True))
    for it in items:
        # The shared self-evolution label map (owner-UX P2-4 final review, item 4)
        # covers the five self-evolution kinds; "correspondent"/"tool_approval"
        # ride separate, non-self-evolution pipelines aggregated into this same
        # list, so they keep their own labels rather than falling back to "skill".
        label = {"correspondent": "contact",
                 "tool_approval": "approval"}.get(
            it["kind"], self_evolution.pending_kind_label(it["kind"]))
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
    """Promote a PENDING proposal to active, or APPROVE a queued tool-approval
    request. KIND is 'self_context', 'skill', or 'tool_approval' (Task 9 / G-2 —
    ITEM_ID is the tap-<id> shown by `owner pending`)."""
    tenant = _owner_tenant(user)
    if kind == "tool_approval":
        from tools.controller.approval_queue import decide_tool_approval
        ok, msg = decide_tool_approval(_goal_board(), item_id, user_id=tenant, approved=True)
        click.echo(click.style(msg, fg="green" if ok else "yellow"))
        if not ok:
            raise SystemExit(1)
        return
    from core import self_evolution
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
    """Reject (archive-then-discard) a PENDING proposal, or DECLINE a queued
    tool-approval request. KIND is 'self_context', 'skill', or 'tool_approval'
    (Task 9 / G-2 — ITEM_ID is the tap-<id> shown by `owner pending`)."""
    tenant = _owner_tenant(user)
    if kind == "tool_approval":
        from tools.controller.approval_queue import decide_tool_approval
        ok, msg = decide_tool_approval(_goal_board(), item_id, user_id=tenant, approved=False)
        click.echo(click.style(msg, fg="green" if ok else "yellow"))
        if not ok:
            raise SystemExit(1)
        return
    from core import self_evolution
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

    Tool-approval requests (Task 9 / G-2) have their OWN surface — see
    `polyrob owner pending` / `owner promote tool_approval <id>` — and are
    excluded here so one isn't shown twice under two different id shapes.
    """
    from agents.task.goals.board import ASK_OPEN
    tenant = _owner_tenant(user)
    rows = [a for a in _goal_board().asks(user_id=tenant, status=ASK_OPEN)
            if (a.payload or {}).get("ask_kind") != "tool_approval"]
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


def _do_approve_all(registry, user_id=None, surface=None):
    """Pure handler (unit-testable without click): approve every PENDING binding
    (optionally scoped to one tenant and/or one surface). Returns the count.
    Approvals go row-by-row with each row's OWN tenant, so the cross-tenant
    promotion guard in registry.approve is never bypassed."""
    approved = 0
    try:
        for r in registry.list(user_id=user_id):
            if r.get("state") != "pending":
                continue
            if surface and r.get("surface") != surface:
                continue
            if registry.approve(surface=r["surface"], address=r["address"],
                                thread_id=r.get("thread_id") or None,
                                user_id=r["user_id"]):
                approved += 1
    except Exception:
        # L10 (2026-07-15): was a silent `except: pass` — a mid-loop registry
        # error would silently under-approve with no signal. Log it; the count
        # returned reflects what actually succeeded.
        logger.warning("owner approve --all: bulk approve failed mid-loop", exc_info=True)
    return approved


@owner.command("approve")
@click.argument("surface", required=False)
@click.argument("address", required=False)
@click.option("--all", "approve_all", is_flag=True, default=False,
              help="Approve ALL pending correspondents (optionally filtered by "
                   "SURFACE argument / --user)")
@click.option("--thread", default=None, help="Thread id (if the correspondent has several)")
@click.option("--user", default=None, help="Scope to one tenant user_id (multi-tenant safety)")
def approve(surface, address, approve_all, thread, user):
    """Approve a PENDING correspondent so their replies route as DATA.

    Single: polyrob owner approve <surface> <address>
    Bulk:   polyrob owner approve --all [<surface>] [--user tenant]
    """
    if approve_all:
        n = _do_approve_all(_registry(_data_dir()), user_id=user, surface=surface)
        color = "green" if n else "yellow"
        click.echo(click.style(f"approved {n} pending correspondent(s)", fg=color))
        return
    if not surface or not address:
        click.echo(click.style(
            "usage: polyrob owner approve <surface> <address>  (or --all)", fg="yellow"))
        raise SystemExit(1)
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
        # L10 (2026-07-15): apply the --status filter IN SQL, before LIMIT 50 —
        # filtering in Python after the LIMIT silently dropped older matching rows
        # (e.g. an old pending invoice past 50 newer completed ones vanished).
        params = ['%"kind": "agent_invoice"%']
        status_clause = ""
        if status:
            status_clause = "AND status = ? "
            params.append(status)
        rows = await db.fetch_all(
            "SELECT * FROM x402_payment_requests WHERE metadata LIKE ? "
            f"{status_clause}ORDER BY created_at DESC LIMIT 50",
            tuple(params))
        import json as _json
        out = []
        for r in rows or []:
            # DatabaseConnection.fetch_* auto-parses JSON-looking TEXT columns,
            # so `metadata` may already be a dict here (json.loads(dict) raises
            # TypeError, not JSONDecodeError — must check isinstance first).
            meta = r.get("metadata")
            if not isinstance(meta, dict):
                try:
                    meta = _json.loads(meta or "{}")
                except Exception:
                    meta = {}
            out.append({"request_id": r["id"], "amount_usd": r.get("amount_usd"),
                        "status": r.get("status"), "purpose": meta.get("purpose"),
                        "payer_contact": meta.get("payer_contact") or meta.get("payer_hint"),
                        "created_at": r.get("created_at")})
        return out

    ok, rows = asyncio.run(_with_bot_db(run))
    if not ok:
        click.echo(click.style(str(rows), fg="yellow"))
        return
    # M16 (2026-07-15): always print the tenant scope so the owner is never confused
    # about which bucket a listing is for (sibling money views default to different
    # scopes — finance/sub resolve one tenant, `owner invoices` no-`--user` = ALL).
    scope = f"tenant {user}" if user else "ALL tenants (use --user to scope)"
    click.echo(click.style(f"invoices — scope: {scope}"
                           + (f" · status={status}" if status else ""), dim=True))
    if not rows:
        click.echo(click.style("no invoices", dim=True))
        return
    for r in rows:
        color = {"pending": "yellow", "completed": "green", "expired": "red"}.get(r["status"], "white")
        line = (f"{click.style(str(r['status']).ljust(10), fg=color)} "
               f"{r['request_id']}  ${float(r['amount_usd'] or 0):.2f}  "
               f"{r.get('purpose') or '(no purpose)'}  ({r.get('created_at')})")
        if r.get("payer_contact"):
            line += f"  billed to: {r['payer_contact']}"
        click.echo(line)


@owner.command("settle")
@click.argument("request_id")
@click.option("--tx-hash", default=None, help="On-chain tx hash, if any")
def settle(request_id, tx_hash):
    """Attest an invoice as PAID (pending -> completed).

    The originating session is woken (payment_settled) ONLY when the settlement
    watcher is actually running — i.e. ``X402_INVOICE_ENABLED=true`` AND a live
    polyrob process is up. This command flips the DB row regardless; if the
    watcher is off, the row is marked completed but no session wake fires until a
    watcher next ticks (L10)."""
    import asyncio

    async def run(db):
        from modules.x402.invoicing import settle_payment_request
        return await settle_payment_request(request_id, transaction_hash=tx_hash, db=db)

    ok, settled = asyncio.run(_with_bot_db(run))
    if not ok:
        click.echo(click.style(str(settled), fg="yellow"))
    elif settled:
        click.echo(click.style(f"settled {request_id}", fg="green"))
        from modules.x402.invoicing import x402_invoicing_enabled
        _wake_on = x402_invoicing_enabled()
        if _wake_on:
            click.echo(click.style(
                "  the settlement watcher will wake the originating session "
                "(if a polyrob process is running).", dim=True))
        else:
            click.echo(click.style(
                "  note: X402_INVOICE_ENABLED is off — the row is completed but NO "
                "session wake will fire until a watcher runs.", fg="yellow"))
    else:
        click.echo(click.style(
            f"{request_id} not settled (unknown id or not pending)", fg="yellow"))


# --- Task 14 (Phase 3 R5): watchtower subscriptions ---------------------

@owner.group("sub")
def sub():
    """Manage watchtower subscriptions (prepaid periods gating a cron job).

    Renewal invoices + the active/grace/suspended lifecycle are driven
    automatically by the settlement watcher (SUBSCRIPTIONS_ENABLED); this
    group is read/admin only.
    """


@sub.command("list")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
def sub_list(user):
    """List this tenant's watchtower subscriptions."""
    import asyncio
    # M16: money listings share ONE resolver with `polyrob finance` (resolve_identity
    # → owner-if-bound else "local"), NOT the instance-id `_owner_tenant` — so the
    # two money views can't disagree on which tenant they read.
    tenant = _money_tenant(user)

    async def run(db):
        from modules.x402 import subscriptions as subs
        return await subs.list_subscriptions(user_id=tenant, db=db)

    ok, rows = asyncio.run(_with_bot_db(run))
    if not ok:
        click.echo(click.style(str(rows), fg="yellow"))
        return
    # M16: always print the tenant scope on the listing.
    click.echo(click.style(f"subscriptions — scope: tenant {tenant}", dim=True))
    if not rows:
        click.echo(click.style("no subscriptions", dim=True))
        return
    color_by_status = {"active": "green", "grace": "yellow",
                       "suspended": "red", "canceled": "white"}
    for r in rows:
        color = color_by_status.get(r["status"], "white")
        click.echo(
            f"{click.style(str(r['status']).ljust(10), fg=color)} "
            f"{r['id']}  ${float(r['amount_usd']):.2f}/{r['period_days']}d  "
            f"cron={r['cron_job_id']}  "
            f"{r['correspondent_surface']}:{r['correspondent_address']}  "
            f"paid_through={r['paid_through']}"
        )


@sub.command("cancel")
@click.argument("subscription_id")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
def sub_cancel(subscription_id, user):
    """Cancel a subscription — its cron job then $0-skips (subscription_lapsed)."""
    import asyncio
    tenant = _money_tenant(user)  # M16: same money resolver as sub_list / finance

    async def run(db):
        from modules.x402 import subscriptions as subs
        return await subs.cancel_subscription(subscription_id, user_id=tenant, db=db)

    ok, canceled = asyncio.run(_with_bot_db(run))
    if not ok:
        click.echo(click.style(str(canceled), fg="yellow"))
        return
    if canceled:
        click.echo(click.style(f"canceled {subscription_id}", fg="green"))
    else:
        click.echo(click.style(
            f"no active subscription '{subscription_id}' for tenant {tenant}", fg="yellow"))


# --- W3: group-chat ingress allowlist (GROUP_CHAT_ENABLED) ---

def _group_allowlist():
    from core.surfaces.group_allowlist import GroupAllowlist
    import os as _os
    return GroupAllowlist(_os.path.join(_data_dir(), "group_allowlist.db"))


@owner.group("groups")
def groups():
    """Manage which group/channel chats the agent may join (default-DENY)."""


@groups.command("allow")
@click.argument("surface")
@click.argument("chat_id")
@click.option("--note", default="", help="Label, e.g. 'dev server #general'")
def groups_allow(surface, chat_id, note):
    """Allow a group chat: polyrob owner groups allow discord <channel_id>."""
    _group_allowlist().allow(surface, chat_id, note=note)
    click.echo(click.style(f"allowed {surface}:{chat_id}", fg="green"))


@groups.command("deny")
@click.argument("surface")
@click.argument("chat_id")
def groups_deny(surface, chat_id):
    """Revoke a group chat."""
    if _group_allowlist().revoke(surface, chat_id):
        click.echo(click.style(f"revoked {surface}:{chat_id}", fg="green"))
    else:
        click.echo(click.style(f"{surface}:{chat_id} was not active", fg="yellow"))


@groups.command("list")
def groups_list():
    """List group-chat allowlist entries."""
    rows = _group_allowlist().list_all()
    if not rows:
        click.echo("no group chats allowed (default-DENY)")
        return
    for r in rows:
        click.echo(f"{r['status']:8} {r['surface']}:{r['chat_id']}"
                   + (f"  — {r['note']}" if r.get("note") else ""))


# ---------------------------------------------------------------------------
# DM pairing (POLYROB_REQUIRE_PAIRING) — 3.11/O5, 2026-07-14 review.
# core/pairing.py issues one-time codes to unknown senders; these commands are
# the operator-side approval path that previously DIDN'T EXIST (the docstring
# pointed at a phantom `rob pair approve`). Same PairingStore + data home the
# surface dispatcher uses.
# ---------------------------------------------------------------------------

def _pairing_store():
    import os as _os

    from core.pairing import PairingStore
    return PairingStore(_os.path.join(_data_dir(), "pairing.db"))


@owner.group("pair")
def pair():
    """Approve/inspect DM pairing requests (POLYROB_REQUIRE_PAIRING)."""


@pair.command("pending")
def pair_pending():
    """List users waiting for pairing approval (with their codes)."""
    rows = _pairing_store().list_pending()
    if not rows:
        click.echo("no pending pairing requests")
        return
    for user_id, code in rows:
        click.echo(f"pending  {user_id}  code={code}"
                   "  (approve: polyrob owner pair approve <code>)")


@pair.command("approve")
@click.argument("code")
def pair_approve(code):
    """Approve the pairing request holding CODE."""
    uid = _pairing_store().approve(code)
    if uid is None:
        raise click.ClickException(f"no pending pairing request with code {code!r}")
    click.echo(click.style(f"paired {uid}", fg="green"))


@pair.command("revoke")
@click.argument("user_id")
def pair_revoke(user_id):
    """Revoke a paired (or pending) user."""
    _pairing_store().revoke(user_id)
    click.echo(click.style(f"revoked {user_id}", fg="green"))
