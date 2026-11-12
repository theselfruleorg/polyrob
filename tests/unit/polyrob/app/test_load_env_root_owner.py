"""Security review 2026-11-10, M7: a ROOT process loads only a root-owned env file.

``<data home>/.env`` sits in a group-writable tree, and the deploy runs root processes
that call ``load_env``. As root, a file another user owns, or one a group/other user can
write, is skipped with one WARN that names it. A non-root process behaves as before.
"""
import logging
import os

import pytest


def _home(tmp_path, monkeypatch, line, mode=0o600):
    home = tmp_path / "h"
    home.mkdir()
    env = home / ".env"
    env.write_text(line + "\n")
    os.chmod(env, mode)
    monkeypatch.setenv("POLYROB_HOME", str(home))
    return env


def _as_root(monkeypatch, file_uid=None):
    """euid 0; with *file_uid*, an fstat of the env file reports that owner."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    if file_uid is None:
        return
    real = os.fstat

    def fstat(fd):
        st = real(fd)
        fields = list(st)
        fields[4] = file_uid           # st_uid
        return os.stat_result(fields)
    monkeypatch.setattr(os, "fstat", fstat)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("M7_PROBE", raising=False)
    yield
    os.environ.pop("M7_PROBE", None)


def test_root_skips_an_env_file_root_does_not_own(tmp_path, monkeypatch, caplog):
    env = _home(tmp_path, monkeypatch, "M7_PROBE=planted")
    _as_root(monkeypatch)              # the test file's owner is not uid 0
    from polyrob.app.bootstrap import load_env
    with caplog.at_level(logging.WARNING, logger="polyrob.app.bootstrap"):
        load_env()
    assert "M7_PROBE" not in os.environ
    hits = [r for r in caplog.records if str(env) in r.getMessage() and "runs as root" in r.getMessage()]
    assert len(hits) == 1


def test_root_skips_a_root_owned_but_group_writable_env_file(tmp_path, monkeypatch, caplog):
    env = _home(tmp_path, monkeypatch, "M7_PROBE=planted", mode=0o660)
    _as_root(monkeypatch, file_uid=0)
    from polyrob.app.bootstrap import load_env
    with caplog.at_level(logging.WARNING, logger="polyrob.app.bootstrap"):
        load_env()
    assert "M7_PROBE" not in os.environ
    assert any(str(env) in r.getMessage() and "group/other-writable" in r.getMessage()
               for r in caplog.records)


def test_root_loads_a_root_owned_private_env_file(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, "M7_PROBE=from_root_file", mode=0o600)
    _as_root(monkeypatch, file_uid=0)
    from polyrob.app.bootstrap import load_env
    load_env()
    assert os.environ.get("M7_PROBE") == "from_root_file"


def test_a_non_root_process_loads_its_file_as_before(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("runs as root")
    _home(tmp_path, monkeypatch, "M7_PROBE=from_home", mode=0o664)
    from polyrob.app.bootstrap import load_env
    load_env()
    assert os.environ.get("M7_PROBE") == "from_home"
