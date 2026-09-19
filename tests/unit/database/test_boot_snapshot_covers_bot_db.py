"""The boot-time pre-migration snapshot must include the DB it is about to migrate.

On a prod-shaped install ``POLYROB_DATA_DIR=/var/lib/polyrob`` while
``DB_PATH=/opt/polyrob/data/database/bot.db``. Only ``UpdateContext.db_paths`` carries
that config-resolved path; ``run_boot_migrations`` used to call ``create_snapshot``
WITHOUT ``db_paths``, so the snapshot fell back to the data-home layouts and backed up
every sidecar except the database being changed. It also now takes the SAME lock
``polyrob update --apply`` holds, so a CLI boot cannot migrate underneath the updater.
"""
import asyncio
from pathlib import Path

import migrations.boot as boot


class _Mgr:
    def __init__(self):
        self.connection = object()


class _Container:
    def __init__(self):
        self._mgr = _Mgr()

    def get_service(self, name):
        return self._mgr if name == "database_manager" else None


def test_boot_snapshot_passes_the_context_db_paths_and_shares_the_update_lock(monkeypatch, tmp_path):
    outside_db = tmp_path / "opt" / "bot.db"
    seen = {}

    class _Ctx:
        snapshots_root = tmp_path / "home" / "snapshots"
        data_home = tmp_path / "home"
        config_paths = []
        dir_paths = []
        db_paths = [outside_db]

    monkeypatch.setattr("cli.update.context.resolve_update_context", lambda **k: _Ctx())

    def fake_create_snapshot(**kw):
        seen.update(kw)
    monkeypatch.setattr("cli.update.snapshot.create_snapshot", fake_create_snapshot)
    monkeypatch.setattr("cli.update.snapshot.prune_snapshots", lambda *a, **k: [])

    async def fake_apply(db, mgr, on_before_change=None, lock_path=None):
        seen["lock_path"] = lock_path
        on_before_change()
        return {"applied": ["1.9.9"], "error": None}
    monkeypatch.setattr(boot, "apply_migrations_at_boot", fake_apply)

    res = asyncio.run(boot.run_boot_migrations(_Container()))
    assert res["applied"] == ["1.9.9"], res
    assert seen["db_paths"] == [outside_db], "boot snapshot must carry the config-resolved DB_PATH"
    assert Path(seen["lock_path"]) == _Ctx.snapshots_root / "update.lock", (
        "boot migrator must take the updater's lock, not a private migrate.lock")
