"""``polyrob owner memory scopes list|show|promote|purge`` — the owner's hand on
memory scopes (a goal or cron run's quarantined findings).

A scope holds what one goal DAG (``goal:<root>``) or one cron job
(``cron:<job>``) learned, invisible to every other session until the root goal
completes verified. The dispatcher promotes automatically; this group is the
escape hatch for the cases it cannot see — a goal the owner completed by hand, a
cron job whose findings are worth keeping, a scope the owner wants gone now
rather than at retention.

Server-side verbs only: no agent-callable action reaches promotion or purge.
Stores resolve through ``cli/_admin_home.admin_data_dir`` (the deployed home)
and the tenant through ``core.admin_data_home.admin_owner_principal``.
"""
import json
import os
import time

import click

from cli._admin_home import as_root_option


def _store(write: bool):
    from cli._admin_home import admin_data_dir
    from modules.memory.sqlite_memory_provider import SqliteMemoryProvider
    db = os.path.join(admin_data_dir(write=write), "memory.db")
    if not os.path.exists(db):
        return None, db
    return SqliteMemoryProvider(db), db


def _tenant(user) -> str:
    if user:
        return str(user)
    from core.admin_data_home import admin_owner_principal
    return admin_owner_principal()


def _day(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except Exception:
        return "-"


def _switch_line() -> str:
    from modules.memory.scope import default_regime, scopes_enabled
    if scopes_enabled():
        return f"memory scopes: ON (goal/cron default regime: {default_regime()})"
    return ("memory scopes: OFF — every row below is ALREADY visible as shared recall "
            "(promote or purge before relying on quarantine)")


@click.group("memory")
def memory():
    """Memory the agent keeps across sessions (owner view)."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()


@memory.group("scopes")
def scopes():
    """Quarantined goal/cron findings: list, show, promote, purge."""


@scopes.command("list")
@click.option("--user", default=None, help="Tenant user_id (default: the bound owner).")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
def scopes_list(user, as_json):
    """Every scope that still holds quarantined findings, newest first."""
    store, db = _store(write=False)
    tenant = _tenant(user)
    if store is None:
        click.echo(f"unavailable(no memory store at {db})")
        return
    try:
        rows = store.list_scopes(tenant)
    except Exception as e:
        click.echo(f"unavailable({type(e).__name__}: {e})")
        return
    if as_json:
        click.echo(json.dumps({"user": tenant, "scopes": rows}, indent=2))
        return
    click.echo(_switch_line())
    if not rows:
        from cli.ui import candy
        click.echo(candy.empty("quarantined memory scopes", yet=False))
        return
    for r in rows:
        click.echo(f"  {r['label']:<24} {r['rows']:>4} row(s)   newest {_day(r['newest_ts'])}")


@scopes.command("show")
@click.argument("label")
@click.option("--user", default=None, help="Tenant user_id (default: the bound owner).")
@click.option("-n", "limit", type=int, default=20, show_default=True)
def scopes_show(label, user, limit):
    """The newest findings held under one scope."""
    store, db = _store(write=False)
    if store is None:
        click.echo(f"unavailable(no memory store at {db})")
        return
    try:
        rows = store.scope_rows(_tenant(user), label, limit=limit)
    except Exception as e:
        click.echo(f"unavailable({type(e).__name__}: {e})")
        return
    if not rows:
        from cli.ui import candy
        click.echo(candy.empty(f"findings under {label}", yet=False))
        return
    for r in rows:
        text = " ".join(str(r["content"] or "").split())[:200]
        click.echo(f"[{_day(r['ts'])}] {text}")


@scopes.command("promote")
@click.argument("label")
@click.option("--user", default=None, help="Tenant user_id (default: the bound owner).")
@click.option("--max-rows", type=int, default=None,
              help="Newest-first cap (default: the automatic promotion cap).")
@as_root_option
def scopes_promote(label, user, max_rows):
    """Move a scope's findings into shared recall (threat-scanned when the scan is on)."""
    store, db = _store(write=True)
    if store is None:
        click.echo(f"unavailable(no memory store at {db})")
        return
    n = store.promote_scope(_tenant(user), label, max_rows=max_rows)
    _audit("promote", label, n)
    click.echo(f"promoted {n} finding(s) from {label} to shared recall")


@scopes.command("purge")
@click.argument("label")
@click.option("--user", default=None, help="Tenant user_id (default: the bound owner).")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@as_root_option
def scopes_purge(label, user, yes):
    """Delete every finding held under one scope."""
    store, db = _store(write=True)
    if store is None:
        click.echo(f"unavailable(no memory store at {db})")
        return
    if not yes:
        click.confirm(f"Delete every finding under {label}?", abort=True)
    n = store.purge_scope(_tenant(user), label)
    _audit("purge", label, n)
    click.echo(f"purged {n} finding(s) from {label}")


def _audit(verb: str, label: str, n: int) -> None:
    """A durable ``memory_write`` row naming the owner act (fail-open)."""
    try:
        from core.event_log import emit
        emit("memory_write", source="owner_cli",
             user_id=_tenant(None),
             attrs={"scope": "memory_scope", "memory_scope": label, "verb": verb,
                    "count": int(n)})
    except Exception:
        pass
