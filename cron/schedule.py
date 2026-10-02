"""Back-compat re-export: the ONE schedule parser lives in ``core.schedule``.

Moved to the core tier (036) so a rail on the goal board and a cron job read the
same owner vocabulary (``30m``, ``every monday 09:00``, 5-field cron, ISO
one-shots) without an agents -> cron import. Import from ``core.schedule`` in
new code.
"""
from core.schedule import (  # noqa: F401 — re-exported by name
    Schedule,
    ScheduleError,
    parse_schedule,
    _expand,
    _parse_cron,
    _parse_every,
)
