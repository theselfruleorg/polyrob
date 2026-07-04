"""T0.2 — DB manifest SSOT.

`core/db_manifest.py` is the single seam backup/rollback consume to enumerate every
SQLite DB POLYROB creates under a data-home. A new sidecar DB is protected by adding
one name here — the update flow never hand-lists DB paths.
"""
from pathlib import Path

import pytest

from core.db_manifest import all_sqlite_dbs, candidate_sqlite_dbs


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")
    return p


def test_candidates_include_bot_and_sidecars(tmp_path):
    cands = candidate_sqlite_dbs(tmp_path)
    names = {p.name for p in cands}
    assert "bot.db" in names
    for sidecar in ("memory.db", "goals.db", "cron.db", "skill_usage.db",
                    "users.db", "tg_dedup.db"):
        assert sidecar in names
    # bot.db lives under database/, sidecars directly under data_home.
    bot = next(p for p in cands if p.name == "bot.db")
    assert bot == (tmp_path / "database" / "bot.db")
    assert (tmp_path / "memory.db") in cands


def test_all_returns_only_existing(tmp_path):
    _touch(tmp_path / "database" / "bot.db")
    _touch(tmp_path / "memory.db")
    _touch(tmp_path / "goals.db")
    # cron.db / skill_usage.db / users.db / tg_dedup.db intentionally absent.

    existing = all_sqlite_dbs(tmp_path)
    names = {p.name for p in existing}
    assert names == {"bot.db", "memory.db", "goals.db"}


def test_paths_absolute_and_deduped(tmp_path):
    _touch(tmp_path / "memory.db")
    existing = all_sqlite_dbs(tmp_path)
    assert all(p.is_absolute() for p in existing)
    assert len(existing) == len(set(existing))


def test_bot_db_override_wins(tmp_path):
    custom = tmp_path / "elsewhere" / "custom.db"
    _touch(custom)
    _touch(tmp_path / "memory.db")
    existing = all_sqlite_dbs(tmp_path, bot_db_path=custom)
    assert custom.resolve() in existing
    # the default database/bot.db is NOT included when an override is given
    assert (tmp_path / "database" / "bot.db") not in existing


def test_alt_bot_db_layouts_are_candidates(tmp_path):
    # The real bot.db path is config-driven (DB_PATH). Prod uses
    # `<root>/data/database/bot.db`, some installs `<root>/data/bot.db` — not the
    # historical guess `<root>/database/bot.db`. All must be snapshot candidates so
    # a rollback never silently skips the live DB (data-loss).
    cands = {str(p) for p in candidate_sqlite_dbs(tmp_path)}
    assert str(tmp_path / "database" / "bot.db") in cands
    assert str(tmp_path / "data" / "bot.db") in cands
    assert str(tmp_path / "data" / "database" / "bot.db") in cands


def test_prod_style_bot_db_is_captured(tmp_path):
    # Only the prod-style location exists; it MUST be backed up.
    _touch(tmp_path / "data" / "database" / "bot.db")
    existing = all_sqlite_dbs(tmp_path)
    assert (tmp_path / "data" / "database" / "bot.db").resolve() in existing


def test_extra_dbs_are_included(tmp_path):
    # An absolute config-resolved DB path outside data_home is captured verbatim.
    external = tmp_path / "opt" / "polyrob" / "data" / "database" / "bot.db"
    _touch(external)
    existing = all_sqlite_dbs(tmp_path, extra_dbs=[external])
    assert external.resolve() in existing


def test_from_runtime_paths(tmp_path):
    _touch(tmp_path / "goals.db")
    from core.db_manifest import all_sqlite_dbs_for

    class _RP:
        data_home = tmp_path

    existing = all_sqlite_dbs_for(_RP())
    assert (tmp_path / "goals.db").resolve() in existing
