"""Autonomy snapshot poll: fail-open, no store creation, correct shape."""
from cli.ui.autonomy_poll import read_autonomy_snapshot


def test_missing_stores_return_none_and_create_nothing(tmp_path):
    snap = read_autonomy_snapshot("local", data_dir=str(tmp_path))
    assert snap is None or snap == {"goals": 0, "cron": 0, "review": True}
    # CRITICAL: polling must never create db files (path-concerns landmine)
    assert not (tmp_path / "cron.db").exists()
    assert not (tmp_path / "goals.db").exists()


def test_never_raises_on_garbage_dir():
    # Contract: fail-open — the call itself must not raise.
    read_autonomy_snapshot("local", data_dir="/nonexistent/nope")
