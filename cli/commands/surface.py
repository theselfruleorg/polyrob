"""polyrob surface — operator controls for per-surface circuit breakers.

The worker process owns an in-memory SurfaceCircuitBreaker that auto-opens after
K consecutive failures. The CLI process is SEPARATE, so pause/resume are persisted
to a tiny SQLite table (data/surface_state.db) that the worker reads on each
is_open() call (when its breaker is built with a CircuitStore attached).

Commands:
    polyrob surface list              Show all known surfaces and their paused state.
    polyrob surface pause <id>        Pause a surface (writes to the store).
    polyrob surface resume <id>       Resume a surface (clears the stored flag).
"""
import os

import click

from cli._admin_home import as_root_option


def _data_dir(write: "bool | None" = None) -> str:
    # The worker's data home via the ONE admin seam, so pause/resume write the
    # SAME surface_state.db the worker reads (cli/_admin_home.py).
    from cli._admin_home import admin_data_dir
    return admin_data_dir(write=write)


def _store(*, write: "bool | None" = None):
    from core.surfaces.circuit import CircuitStore
    return CircuitStore(os.path.join(_data_dir(write), "surface_state.db"))


def _warn_if_breaker_inert() -> None:
    """pause/resume only bite if the worker built a CircuitStore-backed breaker, which
    it does ONLY when OUTBOUND_QUEUE_ENABLED is on. Warn so the operator isn't misled
    by a 'paused' that has no effect."""
    from core.surfaces.config import SurfaceConfig
    if not SurfaceConfig.outbound_queue_enabled():
        click.echo(click.style(
            "note: OUTBOUND_QUEUE_ENABLED is off — the worker builds no circuit breaker, "
            "so this is recorded but has NO effect until you enable it.", fg="yellow"))


@click.group()
def surface():
    """Inspect and control per-surface circuit breakers."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()


def _known_surfaces() -> tuple:
    """Every surface ``SurfaceConfig`` knows a flag for, derived.

    C56: this was a hand-kept literal tuple beside a class that already answers
    the question. A surface added to ``SurfaceConfig`` and not to the tuple was
    simply absent from `surface list --status` — a seat that shows a subset and
    looks complete.
    """
    from core.surfaces.config import SurfaceConfig
    names = sorted(
        attr[: -len("_surface_enabled")]
        for attr in dir(SurfaceConfig)
        if attr.endswith("_surface_enabled")
    )
    return tuple(names)


def _dead_target_counts() -> tuple:
    """``({surface: n}, error)`` from the dead-target registry.

    C12: this opened ``surfaces.db`` and grouped by a column called
    ``surface_id``. The registry lives in ``dead_targets.db`` and its column is
    ``surface`` — so the query raised on EVERY box, was swallowed, and every
    surface reported a confident ``dead-targets: 0``. Read the store through
    its own class, and a fault is NAMED (``None``), never zero.

    A read never CREATES the store: ``DeadTargetStore.__init__`` runs the
    ``CREATE TABLE``, which would materialise a db file just to answer "none".
    """
    db = os.path.join(_data_dir(write=False), "dead_targets.db")
    if not os.path.exists(db):
        return {}, None
    try:
        from core.surfaces.dead_targets import DeadTargetStore
        counts: dict = {}
        for row in DeadTargetStore(db).list_all():
            sid = str(row.get("surface"))
            counts[sid] = counts.get(sid, 0) + 1
        return counts, None
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"


def _surface_status_rows() -> list:
    """030 WS-G3: per-surface health from the DURABLE stores + env (the CLI is
    a separate process from the worker, so the live bus is out of reach —
    enabled flag, pause state, dead-target count and owner address are not).

    Every cell that could not be READ is ``None``, which the renderer prints as
    ``?`` with the reason — never as a zero or a "no".
    """
    from core.surfaces.config import SurfaceConfig
    from core.surfaces.owner_address import owner_address
    paused = {r["surface_id"]: r["paused"] for r in _store(write=False).list_all()}
    dead_counts, dead_error = _dead_target_counts()
    rows = []
    for sid in _known_surfaces():
        enabled = None
        try:
            fn = getattr(SurfaceConfig, f"{sid}_surface_enabled", None)
            enabled = bool(fn()) if callable(fn) else None
        except Exception:
            enabled = None
        # C57: an owner_address() that RAISED rendered exactly like a surface
        # with no owner configured — "owner-addr: none" over a lookup that
        # never ran. Keep the failure distinguishable.
        addr, addr_error = None, None
        try:
            addr = owner_address(None, sid, "")
        except Exception as exc:
            addr_error = f"{type(exc).__name__}: {exc}"
        rows.append({
            "surface_id": sid,
            "enabled": enabled,
            "paused": bool(paused.get(sid, False)),
            "dead_targets": None if dead_error else int(dead_counts.get(sid, 0)),
            "dead_targets_error": dead_error,
            "owner_address_configured": None if addr_error else bool(addr),
            "owner_address_error": addr_error,
        })
    return rows


@surface.command("list")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable JSON.")
@click.option("--status", "with_status", is_flag=True,
              help="Per-surface health: enabled flag, pause state, dead targets, "
                   "owner-address reachability (030 WS-G3).")
def list_surfaces(as_json: bool, with_status: bool):
    """List surfaces and their persisted paused state."""
    if with_status:
        rows = _surface_status_rows()
        if as_json:
            import json
            click.echo(json.dumps(rows, indent=2, default=str))
            return
        for r in rows:
            bits = []
            if r["enabled"] is True:
                bits.append(click.style("enabled", fg="green"))
            elif r["enabled"] is False:
                bits.append(click.style("disabled", dim=True))
            else:
                bits.append("enabled:?")
            if r["paused"]:
                bits.append(click.style("PAUSED", fg="red", bold=True))
            if r["dead_targets"] is None:
                bits.append(click.style("dead-targets:?", fg="yellow"))
            elif r["dead_targets"]:
                bits.append(click.style(f"dead-targets:{r['dead_targets']}", fg="yellow"))
            if r["owner_address_configured"] is None:
                bits.append(click.style("owner-addr:?", fg="yellow"))
            else:
                bits.append("owner-addr:" + ("yes" if r["owner_address_configured"]
                                             else click.style("none", fg="yellow")))
            click.echo(f"  {r['surface_id']:<10} " + "  ".join(bits))
        errs = {r["dead_targets_error"] for r in rows if r["dead_targets_error"]}
        errs |= {r["owner_address_error"] for r in rows if r["owner_address_error"]}
        for e in sorted(errs):
            click.echo(click.style(f"  ? one or more cells could not be read: {e}",
                                   fg="yellow"))
        return
    rows = _store(write=False).list_all()
    if as_json:
        import json
        click.echo(json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        from cli.ui.candy import empty
        click.echo(empty("surface state entries",
                         "a surface is registered here the first time it is paused"))
        return
    for r in rows:
        status = (
            click.style("PAUSED", fg="red", bold=True)
            if r["paused"]
            else click.style("active", fg="green")
        )
        click.echo(f"  {r['surface_id']:<20} {status}")


@surface.command("pause")
@click.argument("surface_id")
@as_root_option
def pause_surface(surface_id: str):
    """Pause a surface — the worker will skip it until resumed."""
    _store(write=True).pause(surface_id)
    click.echo(click.style(f"paused: {surface_id}", fg="yellow"))
    click.echo(click.style("worker will defer outbound messages for this surface", dim=True))
    _warn_if_breaker_inert()


@surface.command("resume")
@click.argument("surface_id")
@as_root_option
def resume_surface(surface_id: str):
    """Resume a paused surface — the worker will start delivering again."""
    _store(write=True).resume(surface_id)
    click.echo(click.style(f"resumed: {surface_id}", fg="green"))
    _warn_if_breaker_inert()
