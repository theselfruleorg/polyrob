"""`polyrob autonomy` — the autonomy mode dial (030 WS-E4, the 026-P3 intent verbs).

`status` renders the effective-posture card (`core.config_policy.posture_card`
— the SAME card `polyrob doctor`, the REPL `/autonomy`, the webview /system
page and Telegram `/status`+`/mode` show), the live halt state, and the
loop-flag list the REPL `/autonomy` panel shows.

`on`/`off` are INTENT verbs: the owner says what they want and the verb writes
the RIGHT flag through the ONE config write path
(``core.config_service.set_value``), echoing its honesty notes (scope
shadowing, the AUTONOMY_MODE clamp echo, "takes effect: restart") instead of
requiring the owner to know which of the four axes to touch. `--mode`
additionally writes AUTONOMY_MODE, validated against the enum SSOT
(``core.config_policy.flag_enums``) before anything is written.

`halt`/`resume` are live aliases of `polyrob owner halt`/`resume` — the same
``core.surfaces.owner_admin`` primitive every seat calls (no restart needed),
with the same verified-state honesty (a halt the runtime cannot see is not a
halt).

Deliberately out of scope: money flags (wallet caps, PAYMENT_APPROVAL_MODE)
and the compute axis (AGENT_COMPUTE_POSTURE) keep their own gates — this
group NEVER writes them.
"""
from __future__ import annotations

import json as _json

import click


@click.group("autonomy")
def autonomy():
    """Autonomy dial: status / on / off / halt / resume."""
    from cli.commands._bootstrap import ensure_env_loaded
    ensure_env_loaded()


def _data_dir() -> str:
    """The SAME data-home resolution `polyrob owner` uses (SSOT seam).

    031: NOT `resolve_data_home()` directly. That resolver never applies the
    server default, so an owner SSHed into the production box with no
    `POLYROB_DATA_DIR` in their shell paused `~/.polyrob` and got a verified
    "Paused everything" the daemon never saw. `core.admin_data_home` adopts the
    deployed home when it can read it and REFUSES when it cannot; a purely local
    box is unchanged and silent.
    """
    from core.admin_data_home import AmbiguousDataHome, admin_data_home
    try:
        return admin_data_home(echo=lambda m: click.echo(click.style(m, fg="yellow"), err=True))
    except AmbiguousDataHome as exc:
        raise click.ClickException(str(exc))


def _write_flag(key: str, value: str, *, is_global: bool) -> None:
    """Route ONE flag write through ``core.config_service.set_value`` and echo
    its message (which already carries the post_write_notes: shadow, clamp
    echo, restart honesty). Raises ``ClickException`` on refusal/invalid."""
    from core.config_service import set_value
    res = set_value(key, value, scope="global" if is_global else "project",
                    surface="local")
    if not res.ok:
        raise click.ClickException(res.message)
    click.echo(res.message)


def _validate_mode(mode: str) -> str:
    """Validate --mode against the enum SSOT BEFORE any write happens."""
    from core.config_policy.flag_enums import enum_error
    err = enum_error("AUTONOMY_MODE", mode)
    if err:
        raise click.ClickException(err)
    return mode.strip().lower()


@autonomy.command("status")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Machine-readable output")
def status_cmd(as_json):
    """Show the effective posture card (all axes), halt state and loop flags.

    Read-only. The card is the ONE builder every seat renders
    (030 WS-E5) — the same lines `polyrob doctor` and Telegram `/mode` show.
    """
    from cli.ui.commands.handlers import _autonomy_snapshot
    from core.config_policy.posture_card import build_posture_card, render_posture_card
    from core.identity import resolve_identity

    user_id = resolve_identity()
    snap = _autonomy_snapshot(user_id, _data_dir())
    _pause = snap.get("pause") or {}  # 031: the ONE pause record (read once, in the snapshot)
    rows = build_posture_card()
    # 2026-08-28 status SSOT: the health block every seat renders, first.
    health_rows: list = []
    health_text: list = []
    try:
        from core.status_snapshot import build_status_snapshot
        from core.status_render import render_health_lines
        status = build_status_snapshot(user_id, data_dir=_data_dir(), include_money=False)
        health_rows = [{"key": h.key, "severity": h.severity, "text": h.text,
                        "remedy": h.remedy} for h in status.health]
        health_text = render_health_lines(status, prefix="  ")
        health_overall = status.overall
        health_unverified = list(status.unavailable_sources)
    except Exception as e:
        health_overall = "unavailable"
        health_unverified = [f"{type(e).__name__}: {str(e)[:120]}"]
        health_text = [f"health: unavailable ({type(e).__name__}: {str(e)[:120]})"]

    if as_json:
        payload = {
            "health": {"overall": health_overall, "items": health_rows,
                       "unverified": health_unverified},
            "posture_card": rows,
            "local_mode": snap["local_mode"],
            "autonomy_enabled": snap["autonomy_enabled"],
            "mode": snap.get("mode_display", "supervised"),
            "posture": snap.get("posture", "silent"),
            "halted": snap.get("halted", False),
            "pause": _pause,
            "loops": {name: bool(val) for name, val in snap["flags"]},
            "open_goals": snap.get("goal_count", 0),
            "cron_jobs": snap.get("cron_count", 0),
        }
        click.echo(_json.dumps(payload, indent=2, default=str))
        return

    for i, line in enumerate(health_text):
        click.echo(click.style(line, bold=True, fg=("red" if health_overall == "degraded" else None))
                   if i == 0 else line)
    click.echo(click.style("effective posture (all axes)", bold=True))
    for line in render_posture_card(rows, prefix="  "):
        click.echo(line)
    from core.status_render import pause_headline_from
    _line = pause_headline_from(_pause, resume_hint="`polyrob autonomy resume`",
                                pause_hint="`polyrob autonomy pause`")
    click.echo(click.style(f"pause: {_line}", fg="red" if _pause.get("paused") else None))
    click.echo(click.style("loops", bold=True))
    for name, val in snap["flags"]:
        click.echo(f"  {name}: {'on' if val else 'off'}")
    click.echo(f"open goals: {snap.get('goal_count', 0)}  "
               f"cron jobs: {snap.get('cron_count', 0)}")
    if not snap["autonomy_enabled"]:
        click.echo(click.style(
            "autonomy is OFF — turn it on with `polyrob autonomy on` "
            "(restart applies)", dim=True))


@autonomy.command("on")
@click.option("--mode", "mode", default=None,
              help="Also set AUTONOMY_MODE (supervised|autonomous)")
@click.option("--global", "is_global", is_flag=True, default=False,
              help="Write to ~/.polyrob/.env (default: ./.polyrob/.env)")
def on_cmd(mode, is_global):
    """Turn the autonomy master ON (writes AUTONOMY_ENABLED=true).

    Env flags configure the NEXT process — the output says so, plus any
    shadow/clamp notes from the one write path. For an immediate un-freeze of
    a paused instance use `polyrob autonomy resume` instead.
    """
    if mode is not None:
        mode = _validate_mode(mode)
    _write_flag("AUTONOMY_ENABLED", "true", is_global=is_global)
    if mode is not None:
        _write_flag("AUTONOMY_MODE", mode, is_global=is_global)


@autonomy.command("off")
@click.option("--mode", "mode", default=None,
              help="Also set AUTONOMY_MODE (supervised|autonomous)")
@click.option("--global", "is_global", is_flag=True, default=False,
              help="Write to ~/.polyrob/.env (default: ./.polyrob/.env)")
def off_cmd(mode, is_global):
    """Turn the autonomy master OFF (writes AUTONOMY_ENABLED=false).

    Takes effect on the next start. To stop autonomous work NOW (no restart),
    use `polyrob autonomy pause`.
    """
    if mode is not None:
        mode = _validate_mode(mode)
    _write_flag("AUTONOMY_ENABLED", "false", is_global=is_global)
    if mode is not None:
        _write_flag("AUTONOMY_MODE", mode, is_global=is_global)
    click.echo(click.style(
        "note: this applies on the next start — to stop autonomous work NOW "
        "run `polyrob autonomy pause`", dim=True))


@autonomy.command("pause")
@click.argument("scopes", nargs=-1)
@click.option("--for", "duration", default=None, help="Auto-resume after e.g. 90m, 6h, 2d")
def pause_cmd(scopes, duration):
    """Pause autonomous work NOW (no restart): everything, or a WORD —
    trading, background, messages, deploying (or a raw scope).

    Writes the ONE 031 pause record (<data>/AUTONOMY_PAUSE.json) every loop,
    timer and on-box script reads. The output is the VERIFIED (read-back) state.
    """
    from core.surfaces.owner_admin import pause_autonomy, render_pause_result
    from core.surfaces.owner_intent import parse_pause_args
    args = list(scopes) + (["for", duration] if duration else [])
    try:
        sc, minutes = parse_pause_args(args)
    except ValueError as e:
        raise click.ClickException(str(e))
    res = pause_autonomy(_data_dir(), scopes=sc, duration_minutes=minutes,
                         reason="polyrob autonomy pause", via="cli")
    click.echo(render_pause_result(res, resume_hint="`polyrob autonomy resume`",
                                   status_hint="`polyrob autonomy status`", chat=False))


@autonomy.command("halt")
def halt_cmd():
    """Alias of `polyrob autonomy pause` (everything)."""
    from core.surfaces.owner_admin import pause_autonomy, render_pause_result
    res = pause_autonomy(_data_dir(), scopes=("all",), reason="polyrob autonomy halt", via="cli")
    click.echo(render_pause_result(res, resume_hint="`polyrob autonomy resume`",
                                   status_hint="`polyrob autonomy status`", chat=False))


@autonomy.command("resume")
@click.argument("scopes", nargs=-1)
def resume_cmd(scopes):
    """Lift the pause (everything, or a WORD/scope)."""
    from core.surfaces.owner_admin import render_resume_result, resume_autonomy_scopes
    from core.surfaces.owner_intent import parse_pause_args
    try:
        sc, minutes = parse_pause_args(list(scopes))
    except ValueError as e:
        raise click.ClickException(str(e))
    if minutes is not None:
        raise click.ClickException("resume takes scopes only (no duration)")
    res = resume_autonomy_scopes(_data_dir(), scopes=None if sc == ("all",) else sc, via="cli")
    click.echo(render_resume_result(res, halt_hint="`polyrob autonomy pause`"))
