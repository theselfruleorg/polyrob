"""/autonomy — the autonomy panel, and `on`/`off` live switches (026 P5).

Extracted from handlers.py per the god-file ratchet; the registry and the
legacy import path (`from cli.ui.commands.handlers import _h_autonomy`) keep
working via handlers' re-export.
"""
from cli.ui.commands.registry import CommandContext
from core.runtime_paths import data_dir_or_home


def _autonomy_snapshot(user_id: str, data_dir: str = "data", sections=None) -> dict:
    """Gather autonomy loop flags + open-goal / cron-job counts into structured data.

    Only the flag-table assembly lives here; the goal/cron COUNTS come from
    ``cli.ui.autonomy_poll.read_autonomy_snapshot`` — the snapshot's ``work`` and
    ``loops`` sections (116 W7.4), read-only: a missing ``cron.db``/``goals.db``
    is 0, never CREATED (the path-concerns landmine). A store that could not be
    read is ``None`` (rendered "unreadable"), never 0. *sections*: a caller that
    already built a snapshot passes its sections (no second read).
    """
    from core.config_policy import AutonomyConfig, autonomy_enabled, local_mode_enabled
    from cli.ui.autonomy_poll import read_autonomy_snapshot
    from core.config_policy.policy import autonomy_mode_display, autonomy_posture

    # 026 P0.5: CRON_ENABLED via its own runtime resolver (posture default
    # honored); fail-open to off — the ticker gate lives in the tools tier.
    try:
        from tools.cronjob_tools import cron_enabled
        cron_flag = bool(cron_enabled())
    except Exception:
        cron_flag = False

    def _gate(fn):
        """043 A8/A42: evaluate a loop's own gate callable (import + call);
        unreadable -> None, rendered "?" rather than a false "off"."""
        try:
            return bool(fn())
        except Exception:
            return None

    def _sandbox_reap_gate():
        from polyrob.app.autonomy_runtime import _sandbox_reap_enabled
        return _sandbox_reap_enabled()

    def _surface_gc_gate():
        from polyrob.app.autonomy_runtime import _surface_gc_enabled
        return _surface_gc_enabled()

    def _quiet_release_gate():
        from polyrob.app.autonomy_runtime import _quiet_release_enabled
        return _quiet_release_enabled()

    def _settlement_gate():
        from polyrob.app.autonomy_runtime import _x402_invoicing_enabled
        return _x402_invoicing_enabled()

    def _bridges_gate():
        from core.wallet import bridge_watcher
        return bridge_watcher.enabled()

    # 043 A8/A42: the panel used to omit the five autonomy_runtime loops that
    # carry no other on/off surface (sandbox-reap, surface-gc, quiet-release,
    # settlement, bridges) — all 8 loops `start_autonomy` can start are named
    # here so the owner can see the same liveness set the status snapshot checks.
    flags = [
        ("self-wake", AutonomyConfig.self_wake_enabled()),
        ("goals", AutonomyConfig.goals_enabled()),
        ("curator", AutonomyConfig.curator_enabled()),
        ("cron", cron_flag),
        ("cron-run-loop", AutonomyConfig.cron_run_loop()),
        ("background-review", AutonomyConfig.background_review_enabled()),
        ("sandbox-reap", _gate(_sandbox_reap_gate)),
        ("surface-gc", _gate(_surface_gc_gate)),
        ("quiet-release", _gate(_quiet_release_gate)),
        ("settlement", _gate(_settlement_gate)),
        ("bridges", _gate(_bridges_gate)),
    ]

    try:
        counts = read_autonomy_snapshot(user_id, data_dir, sections=sections)
    except Exception as e:  # the read failed: unreadable, never zero
        counts = {"goals": None, "cron": None,
                  "unread": {"goals": type(e).__name__, "cron": type(e).__name__}}
    if counts is None:   # nothing to show on the status bar: both stores hold nothing
        counts = {"goals": 0, "cron": 0}

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
        # None = the store could not be read (``counts_unread`` names why).
        "cron_count": counts.get("cron"),
        "goal_count": counts.get("goals"),
        "counts_unread": dict(counts.get("unread") or {}),
    }


def count_text(n) -> str:
    """A count as a seat prints it: the number, or "unreadable" (never 0)."""
    return "unreadable" if n is None else str(n)


async def _switch(ctx: CommandContext, on: bool, is_global: bool) -> None:
    from cli.ui.autonomy_live import autonomy_switch
    ctx.emit(await autonomy_switch(on, is_global=is_global), title="autonomy")


def _h_autonomy(ctx: CommandContext):
    """Show autonomy loop state + cron-job / open-goal counts; `on`/`off` switch
    the loops live (returns an awaitable the dispatcher awaits)."""
    from cli.ui import candy
    from core.status_render import pause_headline_from

    # 026 P5: `/autonomy on|off [--global]` writes AUTONOMY_ENABLED through
    # the one write path WITH a live apply and starts/stops this REPL's loops
    # (no restart). Any other argument still errors and names the verbs.
    if ctx.args:
        verb = ctx.args[0].lower()
        extra = [a for a in ctx.args[1:] if a != "--global"]
        if verb in ("on", "off") and not extra:
            return _switch(ctx, verb == "on", "--global" in ctx.args[1:])
        ctx.emit(
            f"/autonomy takes `on`, `off` or nothing (got: {' '.join(ctx.args)}).\n"
            "`/autonomy on` / `/autonomy off` switch the loops in this session "
            "and persist it; from a shell, `polyrob autonomy on` / "
            "`polyrob autonomy off` (restart applies).\n"
            "For an immediate freeze/unfreeze of all loops use /pause "
            "[everything|trading|background|messages|deploying] [for 6h] "
            "and /resume (live, no restart).",
            title="autonomy",
        )
        return

    # C2: the ONE owner/admin home seam — the same home the daemon's loops
    # write their goals/cron rows into. Read-only (never refuses as root).
    try:
        from cli._admin_home import admin_data_dir
        data_dir = admin_data_dir(write=False)
    except Exception:
        data_dir = data_dir_or_home(None)

    from cli._admin_home import admin_owner_tenant
    snap = _autonomy_snapshot(admin_owner_tenant(ctx.user_id), data_dir)

    rows = [("local mode", "on" if snap["local_mode"] else "off"),
            ("autonomy", "on" if snap["autonomy_enabled"] else "off"),
            ("mode", snap.get("mode_display", "supervised")),
            ("posture", snap.get("posture", "silent")),
            ("pause", pause_headline_from(snap.get("pause") or {}))]
    rows.extend((name, "?" if val is None else ("on" if val else "off"))
               for name, val in snap["flags"])
    lines = [candy.kv_lines(rows), ""]

    # The four answers every seat reads; `/mode all` has the card.
    try:
        from core.config_policy.posture_card import build_answers
        lines.append(candy.section("what the agent may do"))
        lines.extend(f"{candy.GUTTER}{ln}" for ln in build_answers("terminal"))
        lines.append("")
    except Exception:
        pass

    unread = snap.get("counts_unread") or {}
    lines.append(candy.section(f"cron jobs ({count_text(snap['cron_count'])})"))
    if snap["cron_count"] is None:
        lines.append(f"{candy.GUTTER}the cron store could not be read "
                     f"({unread.get('cron') or '?'})")
    elif snap["cron_count"]:
        lines.append(f"{candy.GUTTER}/jobs lists the schedule")

    lines.append("")
    lines.append(candy.section(f"goals (open: {count_text(snap['goal_count'])})"))
    if snap["goal_count"] is None:
        lines.append(f"{candy.GUTTER}the goal board could not be read "
                     f"({unread.get('goals') or '?'})")
    elif snap["goal_count"]:
        lines.append(f"{candy.GUTTER}/goals lists them")

    lines.append("")
    # E18: the ONE remedy, spoken as the verb — never the flag name.
    lines.append(f"{candy.GUTTER}turn it on/off: `/autonomy on` / `/autonomy off` "
                 "(live in this session); from a shell `polyrob autonomy on` / "
                 "`polyrob autonomy off` (restart applies)")

    ctx.emit("\n".join(lines), title="autonomy")
