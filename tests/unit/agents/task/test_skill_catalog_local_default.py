"""`skill_catalog_include_all()` defaults ON everywhere (P2-1a) so the agent can
discover every skill via load_skill rather than only trigger-matched ones — closing
the server-side "unmatched skill is invisible" hole. An explicit env value still wins.
Access-time read, not the import-bound constant.
"""
import importlib

import pytest


@pytest.fixture
def constants(monkeypatch):
    import agents.task.constants as c
    importlib.reload(c)
    return c


def _set(monkeypatch, **env):
    import agents.task.constants as c
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    return c


def test_default_on_on_server(monkeypatch):
    # P2-1a: model-chosen disclosure default-ON even without POLYROB_LOCAL.
    c = _set(monkeypatch, POLYROB_LOCAL=None, SKILL_CATALOG_INCLUDE_ALL=None)
    assert c.skill_catalog_include_all() is True


def test_explicit_off_wins_on_server(monkeypatch):
    c = _set(monkeypatch, POLYROB_LOCAL=None, SKILL_CATALOG_INCLUDE_ALL="false")
    assert c.skill_catalog_include_all() is False


def test_default_on_under_local_mode(monkeypatch):
    c = _set(monkeypatch, POLYROB_LOCAL="1", SKILL_CATALOG_INCLUDE_ALL=None)
    assert c.skill_catalog_include_all() is True


def test_explicit_off_wins_under_local(monkeypatch):
    c = _set(monkeypatch, POLYROB_LOCAL="1", SKILL_CATALOG_INCLUDE_ALL="false")
    assert c.skill_catalog_include_all() is False


def test_explicit_on_wins_on_server(monkeypatch):
    c = _set(monkeypatch, POLYROB_LOCAL=None, SKILL_CATALOG_INCLUDE_ALL="true")
    assert c.skill_catalog_include_all() is True
