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


def _data_dir() -> str:
    # Match the worker's data home via the ONE core policy seam (POLYROB_DATA_DIR
    # wins, else <cwd>/.polyrob) so pause/resume write the SAME surface_state.db the
    # worker reads — 'POLYROB_DATA_DIR or data' pointed the CLI at a divergent ./data.
    from core.runtime_paths import resolve_data_home
    return str(resolve_data_home())


def _store():
    from core.surfaces.circuit import CircuitStore
    return CircuitStore(os.path.join(_data_dir(), "surface_state.db"))


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


_KNOWN_SURFACES = ("telegram", "email", "discord", "slack", "signal", "x", "whatsapp")


def _surface_status_rows() -> list:
    """030 WS-G3: per-surface health from the DURABLE stores + env (the CLI is
    a separate process from the worker, so the live bus is out of reach —
    enabled flag, pause state, dead-target count and owner address are not)."""
    from core.surfaces.config import SurfaceConfig
    from core.surfaces.owner_address import owner_address
    paused = {r["surface_id"]: r["paused"] for r in _store().list_all()}
    dead_counts: dict = {}
    try:
        import sqlite3
        db = os.path.join(_data_dir(), "surfaces.db")
        if os.path.exists(db):
            conn = sqlite3.connect(db)
            try:
                for sid, n in conn.execute(
                        "SELECT surface_id, COUNT(*) FROM dead_targets GROUP BY surface_id"):
                    dead_counts[sid] = n
            finally:
                conn.close()
    except Exception:
        dead_counts = {}
    rows = []
    for sid in _KNOWN_SURFACES:
        enabled = None
        try:
            fn = getattr(SurfaceConfig, f"{sid}_surface_enabled", None)
            enabled = bool(fn()) if callable(fn) else None
        except Exception:
            enabled = None
        addr = None
        try:
            addr = owner_address(None, sid, "")
        except Exception:
            addr = None
        rows.append({
            "surface_id": sid,
            "enabled": enabled,
            "paused": bool(paused.get(sid, False)),
            "dead_targets": int(dead_counts.get(sid, 0)),
            "owner_address_configured": bool(addr),
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
            if r["dead_targets"]:
                bits.append(click.style(f"dead-targets:{r['dead_targets']}", fg="yellow"))
            bits.append("owner-addr:" + ("yes" if r["owner_address_configured"] else
                                          click.style("none", fg="yellow")))
            click.echo(f"  {r['surface_id']:<10} " + "  ".join(bits))
        return
    rows = _store().list_all()
    if as_json:
        import json
        click.echo(json.dumps(rows, indent=2, default=str))
        return
    if not rows:
        click.echo(click.style("no surface state entries", dim=True))
        click.echo(click.style("(surfaces are auto-registered when first paused)", dim=True))
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
def pause_surface(surface_id: str):
    """Pause a surface — the worker will skip it until resumed."""
    _store().pause(surface_id)
    click.echo(click.style(f"paused: {surface_id}", fg="yellow"))
    click.echo(click.style("worker will defer outbound messages for this surface", dim=True))
    _warn_if_breaker_inert()


@surface.command("resume")
@click.argument("surface_id")
def resume_surface(surface_id: str):
    """Resume a paused surface — the worker will start delivering again."""
    _store().resume(surface_id)
    click.echo(click.style(f"resumed: {surface_id}", fg="green"))
    _warn_if_breaker_inert()
