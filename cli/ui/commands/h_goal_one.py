"""h_goal_one.py — ``/goal`` (singular) in the REPL (043 A23).

The WRITE counterpart to the read-only ``/goals``: show / ready / pause /
resume / retry / cancel one goal, plus ``/goal objective …``. Thin over the
SAME ``surfaces.telegram.owner_ops.goal_reply`` the phone calls, so a goal
transition reads and behaves identically on both seats.

⚠️ REACH, never policy (043 global constraint): every transition is the goal
board's own ``update_status`` — this verb adds no authority the board did not
already grant to the owner seat.
"""
from __future__ import annotations

from cli.ui.commands.h_owner import _admin_data_dir, _tenant


def h_goal(ctx) -> None:
    """``/goal <show|ready|pause|resume|retry|cancel> <id>`` — one goal."""
    from surfaces.telegram import owner_ops
    ctx.emit(
        owner_ops.goal_reply(_tenant(ctx), _admin_data_dir(ctx), list(ctx.args or [])),
        title="goal",
    )


HELP_GOAL = (
    "  Act on ONE goal — the write counterpart to the read-only /goals board.\n"
    "\n"
    "    /goal show <id>       full body, priority, failure history\n"
    "    /goal ready <id>      re-queue a blocked/paused goal\n"
    "    /goal pause <id>      hold it (it stops being dispatched)\n"
    "    /goal resume <id>     lift the hold\n"
    "    /goal retry <id>      clear the failure count and re-queue\n"
    "    /goal cancel <id>     stop it (a cancel means never again)\n"
    "\n"
    "  An id prefix is enough when it is unambiguous. See /goals for the board.",
    "`/goal` on Telegram, `polyrob goals`, and the console's Work › Now & next.",
)


def register(reg, Command) -> None:
    """Register ``/goal``. Called from ``h_a23.register``."""
    reg.register(Command(
        "goal", h_goal,
        "Act on one goal: show / ready / pause / resume / retry / cancel",
        usage="<verb> <id>", group="work",
        help_long=HELP_GOAL[0], elsewhere=HELP_GOAL[1],
    ))
