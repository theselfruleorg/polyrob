"""h_rail.py — ``/rail`` in the REPL (036 §4.1).

Thin over the SAME ``surfaces.telegram.rail_ops.rail_reply`` the phone calls, so a
rail reads and behaves identically on both seats (REPL ⊇ Telegram).

⚠️ REACH, never policy: the grant predicate and the rail rules live in
``core/tool_grants.py`` and ``agents/task/goals/rails.py``.
"""
from __future__ import annotations

from cli.ui.commands.h_owner import _admin_data_dir, _tenant


async def h_rail(ctx) -> None:
    """``/rail [list|show|new|edit|on|off|drop|adopt|grant|revoke|grants|templates]``."""
    from surfaces.telegram.rail_ops import rail_reply
    ctx.emit(rail_reply(_tenant(ctx), _admin_data_dir(write=True), list(ctx.args or []),
                        seat="repl"),
             title="rail")


HELP_RAIL = (
    "  Standing work you set up: a rail seeds its legs onto the goal board on a\n"
    "  schedule. Nothing runs until you create one.\n"
    "\n"
    "    /rail                      ON / OFF / UNDECLARED\n"
    "    /rail templates            the offers to start from\n"
    "    /rail new <template> k=v   e.g. /rail new topic-watch topic=\"AI agents\"\n"
    "    /rail show <name>          schedule, legs, grants, last seeds\n"
    "    /rail edit <name> schedule \"every 24h\" | body \"…\" | max 1\n"
    "    /rail on|off <name>        the switch\n"
    "    /rail drop <name>          gone; live legs finish (--cancel-live stops them)\n"
    "    /rail grant <name> <tool>  asks you to confirm; audited\n"
    "    /rail revoke <name> <tool> immediate",
    "`/rail` on Telegram, `polyrob rails`, and export/import as YAML.",
)


def register(reg, Command) -> None:
    """Register ``/rail``. Called from ``h_a23.register``."""
    reg.register(Command(
        "rail", h_rail,
        "Standing work: list, create, switch, drop and grant rails",
        usage="[verb] [name]", group="work",
        help_long=HELP_RAIL[0], elsewhere=HELP_RAIL[1],
    ))
