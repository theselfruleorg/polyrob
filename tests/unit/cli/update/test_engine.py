"""`cli/update/engine.py` — the apply spine: snapshot → install → guarded-migrate →
verify → auto-rollback-on-failure. Runners are injected so the orchestration/rollback
logic is testable without mutating a real install.
"""
import sqlite3

import pytest

from cli.update.context import UpdateContext
from cli.update.engine import UpdateRunners, apply_update


def _seed(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")
    c.executemany("INSERT INTO t DEFAULT VALUES", [() for _ in range(rows)])
    c.commit()
    c.close()


def _count(path):
    c = sqlite3.connect(str(path))
    try:
        return c.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        c.close()


class _Recorder:
    def __init__(self, **behaviors):
        self.calls = []
        self.behaviors = behaviors  # name -> callable or raising Exception

    def _make(self, name):
        def fn():
            self.calls.append(name)
            b = self.behaviors.get(name)
            if isinstance(b, BaseException):
                raise b
            if callable(b):
                b()
        return fn

    def runners(self):
        return UpdateRunners(
            install=self._make("install"),
            migrate=self._make("migrate"),
            verify=self._make("verify"),
            rollback_code=self._make("rollback_code"),
        )


def _ctx(tmp_path, db):
    return UpdateContext(data_home=tmp_path / "home",
                         snapshots_root=tmp_path / "snaps",
                         db_paths=[db])


def test_happy_path_no_rollback(tmp_path):
    db = tmp_path / "home" / "memory.db"
    _seed(db, 10)
    rec = _Recorder()
    res = apply_update(ctx=_ctx(tmp_path, db), runners=rec.runners(),
                       from_version="0.4.2", to_version="0.4.3")
    assert res.ok is True and res.failed_step is None and res.rolled_back is False
    assert rec.calls == ["install", "migrate", "verify"]


def test_install_failure_rolls_back_and_skips_rest(tmp_path):
    db = tmp_path / "home" / "memory.db"
    _seed(db, 10)
    rec = _Recorder(install=RuntimeError("git pull failed"))
    res = apply_update(ctx=_ctx(tmp_path, db), runners=rec.runners(),
                       from_version="0.4.2", to_version="0.4.3")
    assert res.ok is False and res.failed_step == "install" and res.rolled_back is True
    assert "migrate" not in rec.calls and "verify" not in rec.calls
    assert "rollback_code" in rec.calls
    assert _count(db) == 10


def test_migrate_failure_restores_data_and_reverts_code(tmp_path):
    db = tmp_path / "home" / "memory.db"
    _seed(db, 10)

    def bad():
        _seed(db, 5)                       # 15, then boom
        raise RuntimeError("ADD COLUMN failed")

    rec = _Recorder(migrate=None)
    rec.behaviors["migrate"] = bad
    res = apply_update(ctx=_ctx(tmp_path, db), runners=rec.runners(),
                       from_version="0.4.2", to_version="0.4.3")
    assert res.ok is False and res.failed_step == "migrate" and res.rolled_back is True
    assert "verify" not in rec.calls and "rollback_code" in rec.calls
    assert _count(db) == 10                # data restored byte-identical


def test_verify_failure_rolls_back_everything(tmp_path):
    db = tmp_path / "home" / "memory.db"
    _seed(db, 10)

    def mig():
        _seed(db, 5)                       # 15, migration "succeeds"

    rec = _Recorder(verify=RuntimeError("smoke import failed"))
    rec.behaviors["migrate"] = mig
    res = apply_update(ctx=_ctx(tmp_path, db), runners=rec.runners(),
                       from_version="0.4.2", to_version="0.4.3")
    assert res.ok is False and res.failed_step == "verify" and res.rolled_back is True
    assert "rollback_code" in rec.calls
    assert _count(db) == 10                # rolled back to pre-update state
