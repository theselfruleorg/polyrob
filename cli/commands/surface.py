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
    # Match the worker's data home (build_cli_container's _resolve_cli_data_home =
    # <cwd>/.polyrob by default) so pause/resume write the SAME surface_state.db the
    # worker reads — 'POLYROB_DATA_DIR or data' pointed the CLI at a divergent ./data.
    from core.bootstrap import _resolve_cli_data_home
    data_home, _, _ = _resolve_cli_data_home()
    return str(data_home)


def _store():
    from core.surfaces.circuit import CircuitStore
    return CircuitStore(os.path.join(_data_dir(), "surface_state.db"))


def _warn_if_breaker_inert() -> None:
    """pause/resume only bite if the worker built a CircuitStore-backed breaker, which
    it does ONLY when OUTBOUND_QUEUE_ENABLED is on. Warn so the operator isn't misled
    by a 'paused' that has no effect."""
    from agents.task.surface_config import SurfaceConfig
    if not SurfaceConfig.outbound_queue_enabled():
        click.echo(click.style(
            "note: OUTBOUND_QUEUE_ENABLED is off — the worker builds no circuit breaker, "
            "so this is recorded but has NO effect until you enable it.", fg="yellow"))


@click.group()
def surface():
    """Inspect and control per-surface circuit breakers."""


@surface.command("list")
def list_surfaces():
    """List surfaces and their persisted paused state."""
    rows = _store().list_all()
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
