"""/autonomy — the read-only autonomy panel (030 file-size extraction).

Extracted from handlers.py per the god-file ratchet; the registry and the
legacy import path (`from cli.ui.commands.handlers import _h_autonomy`) keep
working via handlers' re-export.
"""
from cli.ui.commands.registry import CommandContext
from core.runtime_paths import data_dir_or_home


def _autonomy_snapshot(user_id: str, data_dir: str = "data") -> dict:
    """Gather autonomy loop flags + open-goal / cron-job counts into structured data.

    Only the flag-table assembly lives here; the goal/cron COUNTS come from
    ``cli.ui.autonomy_poll.read_autonomy_snapshot``, which guards every store read
    with ``os.path.exists`` — a missing ``cron.db``/``goals.db`` is skipped, never
    CREATED (opening a store would mkdir/create the DB as a side effect; the
    path-concerns landmine). Fail-open: a poll error degrades to zero counts
    instead of raising into the REPL.
    """
    from agents.task.constants import AutonomyConfig, autonomy_enabled, local_mode_enabled
    from cli.ui.autonomy_poll import read_autonomy_snapshot
    from core.config_policy.policy import autonomy_mode_display, autonomy_posture

    # 026 P0.5: CRON_ENABLED via its own runtime resolver (posture default
    # honored); fail-open to off — the ticker gate lives in the tools tier.
    try:
        from tools.cronjob_tools import cron_enabled
        cron_flag = bool(cron_enabled())
    except Exception:
        cron_flag = False

    flags = [
        ("self-wake", AutonomyConfig.self_wake_enabled()),
        ("goals", AutonomyConfig.goals_enabled()),
        ("curator", AutonomyConfig.curator_enabled()),
        ("cron", cron_flag),
        ("cron-run-loop", AutonomyConfig.cron_run_loop()),
        ("background-review", AutonomyConfig.background_review_enabled()),
    ]

    try:
        counts = read_autonomy_snapshot(user_id, data_dir) or {}
    except Exception:  # fail-open: stores may not exist yet
        counts = {}

    # 031: the ONE pause record (fail-closed read); `halted` == the `all` scope.
    try:
        from core.surfaces.owner_admin import pause_state
        pause = pause_state(data_dir).to_dict()
    except Exception as e:  # unreadable => paused (the ONE fail-closed shape)
        from core.autonomy_control import unreadable_state
        pause = unreadable_state(type(e).__name__).to_dict()
    halted = bool(pause["paused"] and "all" in pause["scopes"])

    return {
        "local_mode": local_mode_enabled(),
        "autonomy_enabled": autonomy_enabled(),
        "mode_display": autonomy_mode_display(),
        "posture": autonomy_posture(),
        "halted": halted,
        "pause": pause,
        "flags": flags,
        "cron_count": int(counts.get("cron", 0) or 0),
        "goal_count": int(counts.get("goals", 0) or 0),
    }


def _h_autonomy(ctx: CommandContext) -> None:
    """Show autonomy loop state + cron-job / open-goal counts (read-only)."""
    from cli.ui import candy
    from core.status_render import pause_headline_from

    # 026 P0.5: `/autonomy on` used to print the status panel and silently
    # ignore the argument — error honestly and name the real write path.
    if ctx.args:
        ctx.emit(
            f"/autonomy takes no arguments yet (got: {' '.join(ctx.args)}).\n"
            "It is read-only today — to turn autonomy on/off use:\n"
            "  /config set AUTONOMY_ENABLED true   (restart applies)\n"
            "For an immediate freeze/unfreeze of all loops use "
            "/pause [scope…] [for 6h] and /resume (live, no restart).",
            title="autonomy",
        )
        return

    data_dir = "data"
    try:
        cfg = getattr(ctx.container, "config", None)
        data_dir = data_dir_or_home(getattr(cfg, "data_dir", None))
    except Exception:
        pass

    snap = _autonomy_snapshot(ctx.user_id or "local", data_dir)

    rows = [("local mode", "on" if snap["local_mode"] else "off"),
            ("autonomy", "on" if snap["autonomy_enabled"] else "off"),
            ("mode", snap.get("mode_display", "supervised")),
            ("posture", snap.get("posture", "silent")),
            ("pause", pause_headline_from(snap.get("pause") or {}))]
    rows.extend((name, "on" if val else "off") for name, val in snap["flags"])
    lines = [candy.kv_lines(rows), ""]

    # 030 WS-E5: the same effective-posture card every seat renders — including
    # the compute axis, which this panel used to omit entirely.
    try:
        from core.config_policy.posture_card import render_posture_card
        lines.append(candy.section("effective posture (all axes)"))
        lines.extend(f"{candy.GUTTER}{ln}" for ln in render_posture_card())
        lines.append("")
    except Exception:
        pass

    lines.append(candy.section(f"cron jobs ({snap['cron_count']})"))
    if snap["cron_count"]:
        lines.append(f"{candy.GUTTER}/cron lists the schedule")

    lines.append("")
    lines.append(candy.section(f"goals (open: {snap['goal_count']})"))
    if snap["goal_count"]:
        lines.append(f"{candy.GUTTER}/goals lists them")

    lines.append("")
    lines.append(f"{candy.GUTTER}enable/disable: /config set AUTONOMY_ENABLED true|false "
                 "(restart applies)")

    ctx.emit("\n".join(lines), title="autonomy")
