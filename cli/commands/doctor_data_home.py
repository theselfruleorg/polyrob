"""``polyrob doctor``: the data-home split, as a CRITICAL item.

2026-10-29 (a managed user-mode install, 1.3.0 → 1.6.0): the CLI and the
gateway read ``~/.polyrob/data`` while a process without ``POLYROB_LOCAL`` read
``~/.polyrob``; the updater's migrate step created a second, empty bot.db and
the service started on empty stores. That was a log line. Now it is a doctor
item that names every bot.db, which home each kind of process reads, and the
one line that pins them together.

120 W8: the check lives in ``core.health_slot.data_home_split_lines`` (the agent
reads it too). This seat adds only what it alone knows: the installed user
service unit (its path is the installer's, ``cli.commands.service``).
"""
from __future__ import annotations

from pathlib import Path
from typing import List


def _unit_lacks_data_dir() -> "Path | None":
    """The installed user unit, when it predates the POLYROB_DATA_DIR pin."""
    try:
        from cli.commands.service import _systemd_unit_path
        unit = _systemd_unit_path()
        if not unit.is_file():
            return None
        body = unit.read_text(encoding="utf-8")
    except Exception:
        return None
    if "POLYROB_DATA_DIR=" in body or "POLYROB_PROFILE=" in body:
        return None
    return unit


def data_home_split_lines(env: dict) -> List[str]:
    from core.health_slot import data_home_split_lines as _core_lines
    return _core_lines(env, unit_without_pin=_unit_lacks_data_dir())
