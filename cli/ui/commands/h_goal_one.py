"""h_goal_one.py — ``/goal`` (singular) in the REPL (043 A23).

The WRITE counterpart to the read-only ``/goals``: new / show / retry /
pause / stop [--forever] / allow (080 WS-2). Thin over the
SAME ``surfaces.telegram.owner_ops.goal_reply`` the phone calls, so a goal
transition reads and behaves identically on both seats.

⚠️ REACH, never policy (043 global constraint): every transition is the goal
board's own ``update_status`` — this verb adds no authority the board did not
already grant to the owner seat.
"""
from __future__ import annotations

from cli.ui.commands.h_owner import _admin_data_dir, _tenant


async def h_goal(ctx) -> None:
    """``/goal new <what>`` · ``/goal <show|retry|pause|stop [--forever]|allow> <id>``.

    ``async`` + ``_awaited``: the ``owner_ops`` helpers are migrating to async
    one verb at a time (D12), and a seat that hard-codes one shape emits a
    coroutine's repr as if it were the answer the moment its helper flips.
    """
    from surfaces.telegram import owner_ops
    from cli.ui.commands.h_money_verbs import _awaited
    ctx.emit(
        await _awaited(owner_ops.goal_reply(
            _tenant(ctx), _admin_data_dir(write=True), list(ctx.args or []))),
        title="goal",
    )


HELP_GOAL = (
    "  Act on ONE goal — the write counterpart to the read-only /goals board.\n"
    "\n"
    "    /goal new <what>      a background goal (I work on it on my own)\n"
    "    /goal show <id>       why it is where it is, result, cost, session\n"
    "    /goal retry <id>      re-queue a blocked or paused goal (clears failures)\n"
    "    /goal pause <id>      hold it until /goal retry\n"
    "    /goal stop <id>       stop it once (it may be created again)\n"
    "    /goal stop <id> --forever   never again: switched off until /goal allow\n"
    "    /goal allow <id|title>      turn a switched-off goal back on\n"
    "\n"
    "  An id prefix is enough when it is unambiguous. See /goals for the board.",
    "`/goal` on Telegram, `polyrob goals`, and the console's Work › Now & next.",
)


def register(reg, Command) -> None:
    """Register ``/goal``. Called from ``h_a23.register``."""
    reg.register(Command(
        "goal", h_goal,
        "Act on one goal: new / show / retry / pause / stop / allow",
        usage="<verb> <id>", group="work",
        help_long=HELP_GOAL[0], elsewhere=HELP_GOAL[1],
    ))
