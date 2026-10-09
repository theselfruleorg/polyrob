"""SUP-5: a cloned directory's .polyrob/snapshots is never the rollback source."""
from core.runtime_paths import _local_default_data_home as REAL_DEFAULT


def test_local_snapshot_root_lives_in_the_user_home_not_the_cwd(tmp_path, monkeypatch):
    import core.runtime_paths as rp
    from cli.update.context import resolve_update_context

    clone = tmp_path / "clone"
    planted = clone / ".polyrob" / "snapshots" / "20260101T000000Z"
    planted.mkdir(parents=True)
    (planted / "DONE").write_text("")
    home = tmp_path / "home" / ".polyrob"
    monkeypatch.setattr(rp, "_local_default_data_home", REAL_DEFAULT)
    monkeypatch.setenv("POLYROB_HOME", str(home))
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.chdir(clone)

    ctx = resolve_update_context(local=True)
    assert ctx.snapshots_root == (home / "data" / "snapshots").resolve()
    assert not str(ctx.snapshots_root).startswith(str(clone.resolve()))
