"""067 P0.2: doctor reports the lazy-install mode the runtime resolves
(``lazy_deps_mode()``), not a re-derivation from legacy ``LAZY_DEPS_ENABLED``."""
import pytest

from cli.commands.doctor import optional_extras_line


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LAZY_DEPS_MODE", raising=False)
    monkeypatch.delenv("LAZY_DEPS_ENABLED", raising=False)


@pytest.mark.parametrize("rob_local", [False, True])
def test_no_env_reports_trusted_in_both_contexts(rob_local):
    line = optional_extras_line(rob_local=rob_local)
    assert "LAZY_DEPS_MODE=trusted" in line
    assert "install on first use" in line


def test_explicit_off_reports_pip_install(monkeypatch):
    monkeypatch.setenv("LAZY_DEPS_MODE", "off")
    line = optional_extras_line(rob_local=True)
    assert "LAZY_DEPS_MODE=off" in line
    assert "pip install 'polyrob[<extra>]'" in line


def test_legacy_boolean_false_maps_to_off(monkeypatch):
    monkeypatch.setenv("LAZY_DEPS_ENABLED", "false")
    assert "LAZY_DEPS_MODE=off" in optional_extras_line(rob_local=True)


def test_mode_wins_over_legacy_boolean(monkeypatch):
    monkeypatch.setenv("LAZY_DEPS_ENABLED", "false")
    monkeypatch.setenv("LAZY_DEPS_MODE", "legacy")
    assert "LAZY_DEPS_MODE=legacy" in optional_extras_line()
