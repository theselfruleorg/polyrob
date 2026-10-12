"""W8 (2026-10-10): ONE DB registry (core/db_manifest.py::DB_REGISTRY).

Every SQLite file name shipped code opens must have a registry row (or come from
the surface catalog); the backup set and the guard's name set are derived from it.
"""
import re
from pathlib import Path

import pytest

from core import db_manifest as m

REPO = Path(__file__).resolve().parents[3]
ROOTS = ("core", "polyrob", "agents", "tools", "modules", "cli", "api", "cron",
         "surfaces", "webview", "utils", "packs")
_LITERAL = re.compile(r"""["']([a-z0-9_]+\.(?:db|sqlite3?))["']""")


def _shipped_db_literals():
    found = {}
    for root in ROOTS:
        for py in (REPO / root).rglob("*.py"):
            rel = py.relative_to(REPO).as_posix()
            if "/tests/" in rel:
                continue
            for name in _LITERAL.findall(py.read_text(encoding="utf-8", errors="replace")):
                found.setdefault(name, rel)
    return found


def _pack_surface_dbs():
    out = set()
    for toml in (REPO / "packs").rglob("pack.toml"):
        out |= set(re.findall(r"([a-z0-9_]+\.db)", toml.read_text()))
    return out


def test_every_shipped_db_name_is_registered():
    known = m.state_db_names() | _pack_surface_dbs()
    missing = {n: f for n, f in _shipped_db_literals().items() if n not in known}
    assert not missing, (
        "SQLite file(s) opened by shipped code with no row in "
        f"core.db_manifest.DB_REGISTRY: {missing}")


def test_registry_rows_are_well_formed():
    names = [s.name for s in m.DB_REGISTRY]
    assert len(names) == len(set(names)), "duplicate registry row"
    for s in m.DB_REGISTRY:
        assert s.home in ("data", "bot", "wallet", "signer", "session"), s
        owner = REPO / s.owner
        assert owner.is_file(), f"{s.name}: owner module {s.owner} does not exist"
        if s.schema:
            leaf = s.schema.split(".")[-1]
            assert leaf in owner.read_text(), f"{s.name}: {s.schema} not in {s.owner}"


def test_backup_set_is_derived_from_registry():
    data_rows = [s.name for s in m.DB_REGISTRY if s.home == "data" and s.backup]
    assert list(m.SIDECAR_DB_NAMES[:len(data_rows)]) == data_rows
    from core.surfaces.catalog import state_dbs
    assert set(state_dbs()) <= set(m.SIDECAR_DB_NAMES)


def test_state_db_names_covers_every_store():
    names = m.state_db_names()
    for n in ("feedback_replays.db", "telemetry_events.db", "skill_usage.db",
              "users.db", "messages.db", "tg_dedup.db", "signer.sqlite",
              "control.sqlite3", "token_pins.db", "bot.db"):
        assert n in names


def test_db_path_joins_registered_data_stores(tmp_path):
    assert m.db_path("goals.db", tmp_path) == str(tmp_path / "goals.db")
    assert m.db_path("tg_dedup.db", tmp_path) == str(tmp_path / "tg_dedup.db")
    with pytest.raises(KeyError):
        m.db_path("nope.db", tmp_path)
    with pytest.raises(ValueError):
        m.db_path("signer.sqlite", tmp_path)
