"""``polyrob doctor``: the data-home split, as a CRITICAL item.

2026-10-29 (a managed user-mode install, 1.3.0 → 1.6.0): the CLI and the
gateway read ``~/.polyrob/data`` while a process without ``POLYROB_LOCAL`` read
``~/.polyrob``; the updater's migrate step created a second, empty bot.db and
the service started on empty stores. That was a log line. Now it is a doctor
item that names every bot.db, which home each kind of process reads, and the
one line that pins them together.
"""
from __future__ import annotations

from pathlib import Path
from typing import List


def _bot_dbs(homes) -> List[Path]:
    seen, out = set(), []
    for h in homes:
        for c in (h / "database" / "bot.db", h / "data" / "database" / "bot.db"):
            try:
                key = c.resolve()
                if key in seen or not c.is_file() or c.stat().st_size == 0:
                    continue
            except OSError:
                continue
            seen.add(key)
            out.append(key)
    return out


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
    from core.paths import polyrob_home
    from core.runtime_paths import resolve_runtime_paths

    explicit = (env.get("POLYROB_DATA_DIR") or "").strip()
    local = resolve_runtime_paths(local=True).data_home
    server = resolve_runtime_paths(local=False).data_home
    home = polyrob_home()
    homes = [Path(explicit).expanduser()] if explicit else []
    homes += [local, server, home, home / "data"]
    dbs = _bot_dbs(homes)
    reads = (local / "database" / "bot.db").resolve()
    lines: List[str] = []
    if len(dbs) > 1:
        lines.append(
            "CRITICAL data home split: bot.db exists at " + ", ".join(str(d) for d in dbs)
            + f". This process reads {reads}. Stop the services, keep the database that "
            "holds your data, set POLYROB_DATA_DIR=<its home> in the service unit and the "
            ".env file, then restart (the home is the folder that holds database/).")
    if not explicit and local.resolve() != server.resolve():
        lines.append(
            f"CRITICAL data home split: the CLI reads {local}, a process without "
            f"POLYROB_LOCAL (the service, migrate) reads {server}. Set "
            "POLYROB_DATA_DIR=<the home that holds your data> in the service unit and "
            "the .env file.")
    unit = _unit_lacks_data_dir()
    if unit is not None:
        lines.append(
            f"! service unit {unit} does not pin POLYROB_DATA_DIR; run "
            "`polyrob service install` again so the service and the CLI read one home")
    if not lines:
        lines.append(f"data home: {explicit or local} (one home for every process)")
    return lines
