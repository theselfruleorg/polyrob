"""Tests for the shared no-key preflight guard (cli/keys.py::preflight_or_onboard)."""
import cli.keys as keys


import pytest


@pytest.fixture(autouse=True)
def _no_real_env_load(monkeypatch):
    # preflight_or_onboard calls core.bootstrap.load_env — stub it so tests don't
    # mutate os.environ from the dev machine's config files.
    import core.bootstrap as bootstrap
    monkeypatch.setattr(bootstrap, "load_env", lambda *a, **k: "development")


def test_preflight_true_when_key_present(monkeypatch):
    monkeypatch.setattr(keys, "should_warn_no_key", lambda env=None: False)
    assert keys.preflight_or_onboard(interactive=True) is True


def test_preflight_non_interactive_no_key_returns_false_and_prints(monkeypatch, capsys):
    monkeypatch.setattr(keys, "should_warn_no_key", lambda env=None: True)
    assert keys.preflight_or_onboard(interactive=False) is False
    err = capsys.readouterr().err
    assert "No API key found" in err
    assert "deepseek/deepseek-chat" in err  # canonical message reused


def test_preflight_interactive_onboards_then_proceeds(monkeypatch):
    state = {"warn": True}
    monkeypatch.setattr(keys, "should_warn_no_key", lambda env=None: state["warn"])
    monkeypatch.setattr(keys, "_can_prompt", lambda: True)

    import cli.commands.init as init_mod

    def _fake_setup():
        state["warn"] = False  # wizard set a usable key

    monkeypatch.setattr(init_mod, "run_quick_key_setup", _fake_setup)
    assert keys.preflight_or_onboard(interactive=True) is True


def test_preflight_interactive_but_not_a_tty_prints(monkeypatch, capsys):
    # interactive=True but _can_prompt() False (piped/CI) → message + False, no wizard.
    monkeypatch.setattr(keys, "should_warn_no_key", lambda env=None: True)
    monkeypatch.setattr(keys, "_can_prompt", lambda: False)
    assert keys.preflight_or_onboard(interactive=True) is False
    assert "No API key found" in capsys.readouterr().err
