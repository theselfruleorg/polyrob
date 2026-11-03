"""``polyrob owner reopen|refunded`` and the ``invoices --stuck`` listing (098 W1).

The owner's exits for the x402 invoice states no reaper may decide:

* ``invoices --stuck`` lists every ``settling`` row the claim reaper will not
  release (a legacy claim with no lane, a facilitator submission). Each one was
  also escalated to the owner ONCE by the settlement watcher (owner decision Q2).
* ``reopen <id> --confirm`` — owner-attested ``settling → pending`` for a row
  that was never submitted to the facilitator. A submission is refused.
* ``refunded <id> [--tx 0x…] [--note …]`` — ``refund_due → refunded`` (MO-03).
* ``deposit-resolve <id> --sent|--not-sent --confirm`` — decide a stuck sweep (O-02).
* ``invoice-resolve <id> --unpaid|--refund-due --confirm`` — decide a held invoice (O-03).
"""
import asyncio
import json

import click

from cli._admin_home import as_root_option


def _run(coro_factory):
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()
    from cli.commands.owner import _with_bot_db
    return asyncio.run(_with_bot_db(coro_factory))


def print_stuck(tenant, as_json: bool) -> None:
    """The ``--stuck`` listing (called by ``owner invoices``)."""
    async def run(db):
        from modules.x402.invoice_reaper import stuck_settling
        return await stuck_settling(db=db, tenant=tenant)

    ok, rows = _run(run)
    if not ok:
        click.echo(click.style(str(rows), fg="yellow"))
        return
    if as_json:
        click.echo(json.dumps({"stuck": rows}, indent=2, default=str))
        return
    if not rows:
        click.echo("no invoice is held in settling")
        return
    for r in rows:
        lane = "submitted" if r["submitted"] else "claim (no lane)"
        told = "owner told" if r.get("escalated_at") else "not yet escalated"
        click.echo(f"{click.style('settling', fg='yellow')} {r['request_id']}  "
                   f"${float(r.get('amount_usd') or 0):.2f}  {lane}  "
                   f"{r.get('held_reason') or '-'}  {told}  (since {r.get('created_at')})")
    click.echo(click.style(
        "  paid: polyrob owner settle <id> --tx-hash 0x…   "
        "never submitted and not paid: polyrob owner reopen <id> --confirm", dim=True))


@click.command("reopen")
@click.argument("request_id")
@click.option("--confirm", is_flag=True, help="I checked the chain: no payment is behind it.")
@as_root_option
def reopen(request_id, confirm):
    """Make a held invoice payable again (settling -> pending).

    Only for a row that was never submitted to the facilitator. Check the chain
    first: a payment behind a reopened invoice could be charged twice."""
    if not confirm:
        click.echo(click.style("refused: add --confirm after you checked the chain", fg="yellow"))
        return

    async def run(db):
        from modules.x402.invoice_reaper import reopen_claim
        return await reopen_claim(request_id, db=db)

    ok, done = _run(run)
    if not ok:
        click.echo(click.style(str(done), fg="yellow"))
    elif done:
        click.echo(click.style(f"reopened {request_id} (pending)", fg="green"))
    else:
        click.echo(click.style(f"{request_id} not reopened (unknown id, not settling, "
                               "or submitted to the facilitator)", fg="yellow"))


@click.command("invoice-resolve")
@click.argument("request_id")
@click.option("--unpaid", is_flag=True, help="The chain shows no payment: void it (expired).")
@click.option("--refund-due", "refund_due", is_flag=True,
              help="The payer was charged for nothing proven: record a refund owed.")
@click.option("--confirm", is_flag=True, help="I checked the chain.")
@as_root_option
def invoice_resolve(request_id, unpaid, refund_due, confirm):
    """Decide a held invoice no reaper can decide.

    For a row submitted to the facilitator and still `settling`, or an unproven
    `settled_no_tx`. --unpaid voids it (only after the payer's authorization
    deadline); --refund-due records the refund you owe (close it later with
    `owner refunded`). A never-submitted claim: use `owner reopen`."""
    if unpaid == refund_due:
        raise click.UsageError("say --unpaid or --refund-due")
    if not confirm:
        raise click.UsageError("check the chain first, then add --confirm")

    async def run(db):
        from modules.x402.invoice_reaper import resolve_held
        return await resolve_held(request_id, paid=refund_due, db=db)

    ok, done = _run(run)
    if not ok:
        click.echo(click.style(str(done), fg="yellow"))
    elif done:
        click.echo(click.style(f"{request_id}: " + ("refund_due" if refund_due else "expired"),
                               fg="green"))
    else:
        click.echo(click.style(
            f"{request_id} not changed (unknown id, not held, or — for --unpaid — the "
            "payer's authorization deadline has not passed yet)", fg="yellow"))


@click.command("refunded")
@click.argument("request_id")
@click.option("--tx", "tx_hash", default=None, help="The refund transaction hash, if any.")
@click.option("--note", default=None, help="A short note (how the payer was refunded).")
@as_root_option
def refunded(request_id, tx_hash, note):
    """Record that a refund_due payment was refunded (refund_due -> refunded)."""
    async def run(db):
        from modules.x402.x402_integration import mark_payment_refunded
        return await mark_payment_refunded(request_id, tx_hash=tx_hash, note=note, db=db)

    ok, done = _run(run)
    if not ok:
        click.echo(click.style(str(done), fg="yellow"))
    elif done:
        click.echo(click.style(f"refunded {request_id}", fg="green"))
    else:
        click.echo(click.style(f"{request_id} not changed (unknown id or not refund_due)",
                               fg="yellow"))


@click.command("deposits")
@click.option("--user", default=None, help="One tenant's deposits (default: all).")
@click.option("--onchain", is_flag=True,
              help="Also read each deposit address balance on chain now (ETHEREUM_RPC_URL).")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def deposits(user, onchain, as_json):
    """Reconcile credited deposits, their sweeps and the balance marks (read-only).

    098 MO-01: per (user, chain, token): credited deposits by sweep state, the
    deposit monitor's balance mark and its block, and with --onchain the
    address balance now. A sweep `sweeping` (outcome unknown), `sweep_failed`
    or `swept_unproven` (remote signer, no receipt) needs you; nothing here
    moves money or credits anyone.
    """
    async def run(db):
        from modules.payments.sweep_state import deposit_report
        reader = await _onchain_reader(db) if onchain else None
        return await deposit_report(db, user_id=user, onchain=reader)

    ok, rows = _run(run)
    if not ok:
        click.echo(click.style(str(rows), fg="yellow"))
        return
    if as_json:
        click.echo(json.dumps({"deposits": rows}, indent=2, default=str))
        return
    if not rows:
        click.echo("no credited deposits")
        return
    for r in rows:
        states = ", ".join(f"{s} {v['count']} (${v['usd']:.2f})"
                           for s, v in sorted(r["states"].items()))
        mark = ("no mark" if r["mark_balance"] is None
                else f"mark {r['mark_balance']} @ block {r['mark_as_of_block']}")
        chain_now = "" if not onchain else (
            "  on chain: unknown" if r["onchain_balance"] is None
            else f"  on chain: {r['onchain_balance']}")
        flag = click.style("  ⚠ needs you", fg="yellow") if r["needs_owner"] else ""
        click.echo(f"{r['user_id']} {r['chain']} {r['token']}: credited ${r['credited_usd']:.2f}"
                   f" — {states}; {mark}{chain_now}{flag}")


@click.command("deposit-resolve")
@click.argument("deposit_id", type=int)
@click.option("--not-sent", "not_sent", is_flag=True,
              help="Nothing left the deposit address: release the group (it is swept again).")
@click.option("--sent", is_flag=True, help="The sweep tx landed: record it (no receipt booked).")
@click.option("--tx", "tx_hash", default=None, help="The sweep tx hash (default: the recorded one).")
@click.option("--confirm", is_flag=True, help="I checked the deposit address on chain.")
@as_root_option
def deposit_resolve(deposit_id, not_sent, sent, tx_hash, confirm):
    """Decide a deposit sweep stuck in `sweeping`.

    The outcome of its broadcast is unknown, so no reaper moves it, and while it
    stands the user's later deposits of that token are not swept. Check the
    deposit address (`owner deposits --onchain`, or the recorded tx), then say
    what happened: --not-sent releases the group, --sent records the tx."""
    if sent == not_sent:
        raise click.UsageError("say --sent or --not-sent")
    if not confirm:
        raise click.UsageError("check the chain first, then add --confirm")

    async def run(db):
        from modules.payments.sweep_state import resolve_stuck_sweep
        try:
            return await resolve_stuck_sweep(db, deposit_id, sent=sent, tx_hash=tx_hash)
        except ValueError as exc:
            raise click.UsageError(str(exc))

    ok, n = _run(run)
    if not ok:
        click.echo(click.style(str(n), fg="yellow"))
    elif n:
        click.echo(click.style(f"deposit {deposit_id}: {n} row(s) "
                               + ("recorded swept_unproven" if sent else "released to unswept"),
                               fg="green"))
        _say_open_wallet_submissions()
    else:
        click.echo(click.style(f"deposit {deposit_id} not changed (not sweeping, or unknown id)",
                               fg="yellow"))


def _say_open_wallet_submissions() -> None:
    """098 review: a local-signer sweep leaves its wallet submission row open,
    and an open row refuses EVERY later wallet send (the next sweep included).
    Name each one and the verb that closes it. Fail-open: an unreadable journal
    is said, not shown as empty."""
    try:
        from core.wallet.submission_journal import unresolved
        rows = unresolved()
    except Exception as exc:  # noqa: BLE001
        click.echo(click.style(f"wallet submission journal unreadable ({type(exc).__name__}) — "
                               "check `polyrob wallet` before the next send", fg="yellow"))
        return
    for r in rows or []:
        ref = r.get("tx_hash") or r.get("reference") or "?"
        click.echo(click.style(
            f"open wallet submission {ref} ({r.get('state')}) blocks every wallet send: "
            "close it with `polyrob wallet release-submission <ref> --never-sent --reason …` "
            "(nothing left) or `polyrob wallet book <ref>` (it was sent)", fg="yellow"))


async def _onchain_reader(db):
    """``(user, chain, token) -> balance`` at the finalized block (wei for ETH,
    token units otherwise), or None when it cannot be read."""
    import os
    from types import SimpleNamespace
    from modules.payments.deposit_monitor import DepositMonitor
    mon = DepositMonitor(db, None, SimpleNamespace(
        ethereum_rpc_url=os.environ.get("ETHEREUM_RPC_URL")))
    cache = {}

    async def read(user_id, chain, token):
        cfg = mon.chains.get(chain) or {}
        if not cfg.get("rpc_url"):
            return None
        row = await db.fetch_one("SELECT deposit_address FROM user_deposit_addresses "
                                 "WHERE user_id = ?", (user_id,))
        if not row:
            return None
        key = (row["deposit_address"], chain)
        if key not in cache:
            cache[key] = {d["token_symbol"]: d["amount"] for d in
                          await mon._check_chain_deposits(row["deposit_address"], chain, cfg)}
        return cache[key].get(token)
    return read


@click.command("reactivate")
@click.argument("subscription_id")
@click.option("--user", default=None, help="Tenant user_id (default: bound owner / 'local')")
@as_root_option
def sub_reactivate(subscription_id, user):
    """Reactivate a grace/suspended subscription by hand — active
    again for one period from now; its cron job runs on the next tick. No
    payment is taken or recorded. A canceled subscription stays canceled."""
    from cli.commands.owner import _money_tenant, _warn_if_subscriptions_off, _with_bot_db
    tenant = _money_tenant(user)  # M16: same money resolver as sub_list / finance

    async def run(db):
        from modules.x402 import subscriptions as subs
        return await subs.reactivate_subscription(subscription_id, user_id=tenant, db=db)

    ok, res = asyncio.run(_with_bot_db(run))
    if not ok:
        click.echo(click.style(str(res), fg="yellow"))
        raise SystemExit(1)
    done, detail = res
    if done:
        import datetime as _dt
        until = _dt.datetime.fromtimestamp(int(detail), _dt.timezone.utc)
        click.echo(click.style(f"reactivated {subscription_id} — paid through "
                               f"{until:%Y-%m-%d %H:%MZ}", fg="green"))
    else:
        click.echo(click.style(
            f"{subscription_id} not reactivated for tenant {tenant}: {detail}", fg="yellow"))
        raise SystemExit(1)
    _warn_if_subscriptions_off()
