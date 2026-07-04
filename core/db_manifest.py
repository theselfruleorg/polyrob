"""SSOT for the set of SQLite databases POLYROB creates under a data-home.

The `polyrob update` backup/rollback flow must snapshot *every* user database, not
just the relational `bot.db` that the migration system knows about. The autonomy,
memory, and surface layers each open their own sidecar DB directly under the data
home (`memory.db`, `goals.db`, `cron.db`, `skill_usage.db`, `users.db`,
`tg_dedup.db`) with ad-hoc `CREATE TABLE IF NOT EXISTS` — invisible to
`migrations/`. This module is the single place that enumerates them, so protecting a
new sidecar DB is a one-line change here and backup/restore stay in sync.

Layout (see modules/memory/backend_factory.py, agents/task/goals/board.py,
cron/runner.py, modules/skills/skill_usage.py, surfaces/telegram/harness.py):
- `bot.db`      -> ``<data_home>/database/bot.db``  (modules/database/database_manager.py)
- everything else -> ``<data_home>/<name>``
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Union

# Sidecar DBs opened directly under the data-home by the autonomy/memory/surface layers.
SIDECAR_DB_NAMES = (
    "memory.db",
    "goals.db",
    "cron.db",
    "skill_usage.db",
    "users.db",
    "tg_dedup.db",
)

_PathLike = Union[str, Path]


# bot.db is NOT at a single fixed path: its real location is config-driven (the
# ``DB_PATH`` env / ``AgentConfig.db_path``, default ``data/bot.db`` anchored to the
# data-home). Prod ships ``DB_PATH=<root>/data/database/bot.db``; the historical guess
# was ``<root>/database/bot.db``. When we can't resolve the config value we must treat
# ALL known layouts as candidates so a snapshot never silently skips the live DB — a
# missed bot.db means a rollback wipes the user's real data (data-loss).
_BOT_DB_RELATIVE_LAYOUTS = (
    ("database", "bot.db"),
    ("data", "bot.db"),
    ("data", "database", "bot.db"),
)


def candidate_sqlite_dbs(
    data_home: _PathLike,
    *,
    bot_db_path: Optional[_PathLike] = None,
    extra_dbs: Optional[List[_PathLike]] = None,
) -> List[Path]:
    """Every SQLite DB path POLYROB *may* create under ``data_home``.

    Returns absolute, de-duplicated paths whether or not they exist yet. ``bot_db_path``
    overrides the bot.db candidates with a single explicit path (e.g. an absolute
    config-resolved ``DB_PATH``); when given, the default layouts are not included.
    ``extra_dbs`` are always appended (e.g. a config-resolved DB that lives OUTSIDE
    ``data_home``, such as prod's ``/opt/polyrob/...``).
    """
    home = Path(data_home).resolve()
    if bot_db_path:
        bot_cands: List[Path] = [Path(bot_db_path).resolve()]
    else:
        bot_cands = [home.joinpath(*layout) for layout in _BOT_DB_RELATIVE_LAYOUTS]
    paths: List[Path] = list(bot_cands)
    paths.extend(home / name for name in SIDECAR_DB_NAMES)
    if extra_dbs:
        paths.extend(Path(p) for p in extra_dbs)

    seen: set = set()
    out: List[Path] = []
    for p in paths:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out


def all_sqlite_dbs(
    data_home: _PathLike,
    *,
    bot_db_path: Optional[_PathLike] = None,
    extra_dbs: Optional[List[_PathLike]] = None,
) -> List[Path]:
    """The subset of :func:`candidate_sqlite_dbs` that currently exists on disk.

    This is what backup/rollback iterate over — only real files are snapshotted.
    """
    return [
        p for p in candidate_sqlite_dbs(
            data_home, bot_db_path=bot_db_path, extra_dbs=extra_dbs)
        if p.is_file()
    ]


def all_sqlite_dbs_for(paths, *, bot_db_path: Optional[_PathLike] = None) -> List[Path]:
    """Convenience wrapper accepting a ``RuntimePaths``-like object (``.data_home``)."""
    return all_sqlite_dbs(paths.data_home, bot_db_path=bot_db_path)
