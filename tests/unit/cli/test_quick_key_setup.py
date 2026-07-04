"""Tests for the inline key wizard (cli/commands/init.py::run_quick_key_setup)."""
import os

import cli.commands.init as init_mod
from modules.llm.profiles import all_profiles


def test_run_quick_key_setup_openrouter_first(monkeypatch, tmp_path):
    # OpenRouter is FIRST in PROFILES order → the first prompt collects it; the rest blank.
    responses = iter(["sk-or-test-0123456789abcdef"] + [""] * (len(all_profiles()) - 1))
    monkeypatch.setattr(init_mod.click, "prompt", lambda *a, **k: next(responses))
    monkeypatch.setattr("core.paths.polyrob_home", lambda: tmp_path)
    # Isolate os.environ mutation (function sets keys directly); auto-restored by monkeypatch.
    monkeypatch.setattr(os, "environ", dict(os.environ))
    os.environ.pop("OPENROUTER_API_KEY", None)

    ok = init_mod.run_quick_key_setup()

    assert ok is True
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-test-0123456789abcdef"
    assert "OPENROUTER_API_KEY=sk-or-test-0123456789abcdef" in (tmp_path / ".env").read_text()


def test_run_quick_key_setup_rejects_too_short_key(monkeypatch, tmp_path):
    # A too-short/placeholder key is 'present' but NOT usable — it would pass a
    # presence gate then crash the LLM manager. run_quick_key_setup must return False
    # so onboarding warns instead of claiming success.
    order = [p.env_key for p in all_profiles()]
    responses = iter(["short" if k == "OPENROUTER_API_KEY" else "" for k in order])
    monkeypatch.setattr(init_mod.click, "prompt", lambda *a, **k: next(responses))
    monkeypatch.setattr("core.paths.polyrob_home", lambda: tmp_path)
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for k in order:
        os.environ.pop(k, None)

    ok = init_mod.run_quick_key_setup()

    assert ok is False  # "short" < 20 chars -> not a usable key


def test_run_quick_key_setup_deepseek_only_returns_false(monkeypatch, tmp_path):
    # Feeding ONLY a deepseek key → not a usable key → returns False (still warns).
    order = [p.env_key for p in all_profiles()]
    responses = iter(["sk-ds" if k == "DEEPSEEK_API_KEY" else "" for k in order])
    monkeypatch.setattr(init_mod.click, "prompt", lambda *a, **k: next(responses))
    monkeypatch.setattr("core.paths.polyrob_home", lambda: tmp_path)
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for k in order:
        os.environ.pop(k, None)

    ok = init_mod.run_quick_key_setup()

    assert ok is False
    assert os.environ["DEEPSEEK_API_KEY"] == "sk-ds"  # persisted, just not "usable"
