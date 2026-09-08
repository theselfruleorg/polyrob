"""The autouse pause-record isolation (`tests/conftest.py::
_isolate_autonomy_pause_record`) must hold, and must keep its escape hatches.

``core.autonomy_control.state_bases`` ALWAYS appends the resolved data home,
even when an explicit ``data_dir`` is passed — so a test that calls
``ac.pause(tmp)`` without hand-patching the resolver writes a REAL
``AUTONOMY_PAUSE.json`` into the developer's ``cwd/.polyrob``. ``read_state`` is
fail-CLOSED, so that leak makes every later goals/cron/money/social test in the
session honestly refuse to run.
"""
from pathlib import Path


def test_pause_never_reaches_the_developers_real_data_home(tmp_path, monkeypatch):
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    from core import autonomy_control as ac
    from core.runtime_paths import resolve_data_home

    real = Path(resolve_data_home())
    bases = ac.state_bases(str(tmp_path))
    assert str(real) not in bases, (
        f"the REAL data home {real} is a pause-record base inside a test")

    ac.pause(str(tmp_path), via="test")
    assert (tmp_path / ac.PAUSE_FILENAME).exists(), "the test's own base still works"
    assert not (real / ac.PAUSE_FILENAME).exists(), (
        f"a unit test paused the developer's real data home ({real})")


def test_env_pinned_data_home_still_uses_the_real_resolution(tmp_path, monkeypatch):
    """Escape hatch 1: a test that pins POLYROB_DATA_DIR means it."""
    pinned = tmp_path / "pinned"
    monkeypatch.setenv("POLYROB_DATA_DIR", str(pinned))
    from core import autonomy_control as ac

    assert str(pinned.resolve()) in ac.state_bases(None)


def test_patched_resolver_still_wins(tmp_path, monkeypatch):
    """Escape hatch 2: a test that monkeypatches ``resolve_data_home`` means it
    (the shape every existing 031 test uses)."""
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    pinned = tmp_path / "pinned2"
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: pinned)
    from core import autonomy_control as ac

    assert str(pinned) in ac.state_bases(None)
