"""TDD tests for core.env — canonical env-flag parser."""
from core.env import bool_env, int_env


def test_bool_env_falsey_set(monkeypatch):
    monkeypatch.setenv("X", "off");    assert bool_env("X", True)  is False
    monkeypatch.setenv("X", "none");   assert bool_env("X", True)  is False
    monkeypatch.setenv("X", "false");  assert bool_env("X", True)  is False
    monkeypatch.setenv("X", "0");      assert bool_env("X", True)  is False
    monkeypatch.setenv("X", "no");     assert bool_env("X", True)  is False
    monkeypatch.setenv("X", "");       assert bool_env("X", True)  is True   # blank → default
    monkeypatch.setenv("X", "true");   assert bool_env("X", False) is True
    monkeypatch.setenv("X", "on");     assert bool_env("X", False) is True
    monkeypatch.setenv("X", "1");      assert bool_env("X", False) is True
    monkeypatch.setenv("X", "yes");    assert bool_env("X", False) is True
    monkeypatch.delenv("X", raising=False)
    assert bool_env("X", True)  is True
    assert bool_env("X", False) is False


def test_bool_env_case_insensitive(monkeypatch):
    monkeypatch.setenv("X", "OFF");    assert bool_env("X", True)  is False
    monkeypatch.setenv("X", "True");   assert bool_env("X", False) is True
    monkeypatch.setenv("X", "FALSE");  assert bool_env("X", True)  is False


def test_int_env(monkeypatch):
    monkeypatch.setenv("N", "5");  assert int_env("N", 1) == 5
    monkeypatch.setenv("N", "x");  assert int_env("N", 1) == 1   # non-int → default
    monkeypatch.delenv("N", raising=False)
    assert int_env("N", 1) == 1
    assert int_env("N", 99) == 99
