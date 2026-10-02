"""M12 (2026-09-23): an env write can never smuggle a second line or a bad key."""
from pathlib import Path

import pytest

from core import config_service
from core.env_file import env_write_error, read_env_file, upsert_env_var


@pytest.mark.parametrize("bad", ["1\nPOLYROB_LOCAL=1", "1\r\nX=1", "1\rX=1", "a\0b"])
def test_upsert_refuses_line_breakers(tmp_path, bad):
    p = tmp_path / ".env"
    with pytest.raises(ValueError):
        upsert_env_var(p, "AUTONOMY_MODE", bad)
    assert not p.exists()


@pytest.mark.parametrize("key", ["lower", "1ABC", "A-B", "A B", "A=B", "", "_X"])
def test_upsert_refuses_bad_keys(tmp_path, key):
    with pytest.raises(ValueError):
        upsert_env_var(tmp_path / ".env", key, "1")


def test_upsert_accepts_a_normal_pair(tmp_path):
    p = tmp_path / ".env"
    upsert_env_var(p, "SOME_FLAG_2", "value with spaces=and equals")
    assert read_env_file(p) == {"SOME_FLAG_2": "value with spaces=and equals"}
    assert env_write_error("SOME_FLAG_2", "x") == ""


@pytest.mark.parametrize("surface", ["console", "local"])
def test_set_value_refuses_newline_injection(tmp_path, monkeypatch, surface):
    monkeypatch.chdir(tmp_path)
    from core.flags import REGISTRY
    key = next(k for k in REGISTRY if not config_service.is_console_unwritable(k))
    res = config_service.set_value(key, "true\nPOLYROB_LOCAL=1", scope="project",
                                   surface=surface)
    assert res.ok is False
    assert res.outcome == "invalid"
    env = Path(tmp_path) / ".polyrob" / ".env"
    assert not env.exists() or "POLYROB_LOCAL" not in env.read_text()


def test_set_value_refuses_newline_in_a_pref(tmp_path):
    res = config_service.set_value("ui.theme", "dark\nx", user_id="u1",
                                   home_dir=str(tmp_path))
    assert res.ok is False and res.outcome == "invalid"
