"""Seam 3 — the retired local-mode provider-key backfill.

The automatic import of provider keys from config/.env.{production,development}
is gone (`polyrob config migrate` is the explicit replacement). What stays: a
one-time WARN that names the migrate verb when the old path WOULD have fired.
No entry point reads config/.env.* (080 WS-1: one env ladder).
"""
import os

import pytest

from polyrob.app import bootstrap


def _write(p, body):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


# --- load_env integration (mutates os.environ → snapshot/restore) ---

@pytest.fixture
def clean_environ(monkeypatch, tmp_path):
    """Run with no provider keys in env, HOME/cwd isolated to tmp, env restored."""
    for k in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "NVIDIA_API_KEY",
        "CONFIG_ENV", "ENV",
    ):
        monkeypatch.delenv(k, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    work = tmp_path / "work"
    (work / "config").mkdir(parents=True)
    monkeypatch.chdir(work)
    return work


# W1.2: the backfill is retired; a one-time WARN names `polyrob config migrate`.


def test_load_env_default_does_not_backfill_and_warns_once(clean_environ, monkeypatch, caplog):
    import logging
    _write(clean_environ / "config" / ".env.production", "OPENROUTER_API_KEY=sk-or-z\n")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(bootstrap, "_warned_backfill_retired", False)
    with caplog.at_level(logging.WARNING, logger="polyrob.app.bootstrap"):
        bootstrap.load_env()
    assert os.environ.get("OPENROUTER_API_KEY") is None
    warned = [r for r in caplog.records if "polyrob config migrate" in r.getMessage()]
    assert warned, "the retired backfill must name the migrate verb when it WOULD have fired"
    # one-time: a second load_env must stay silent
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="polyrob.app.bootstrap"):
        bootstrap.load_env()
    assert not [r for r in caplog.records if "polyrob config migrate" in r.getMessage()]


def test_load_env_no_warn_when_a_usable_key_exists(clean_environ, monkeypatch, caplog):
    import logging
    _write(clean_environ / "config" / ".env.production", "OPENROUTER_API_KEY=sk-or-z\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-have-one-0123456789")
    monkeypatch.setattr(bootstrap, "_warned_backfill_retired", False)
    with caplog.at_level(logging.WARNING, logger="polyrob.app.bootstrap"):
        bootstrap.load_env()
    assert not [r for r in caplog.records if "polyrob config migrate" in r.getMessage()]


def test_load_env_never_reads_config_env_files(clean_environ, monkeypatch):
    _write(clean_environ / "config" / ".env.production", "OPENROUTER_API_KEY=sk-or-z\n")
    bootstrap.load_env("production")
    assert os.environ.get("OPENROUTER_API_KEY") is None
