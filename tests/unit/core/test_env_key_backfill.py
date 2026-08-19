"""Seam 3 — local-mode provider-key backfill (Phase 0b).

When the CLI (local_mode) would otherwise have zero provider keys, import ONLY
secret keys from config/.env.{production,development} — never flags. Server path
(local_mode=False) is never touched.
"""
import os

import pytest

from core import bootstrap


def _write(p, body):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


# --- the dict-testable helper (no os.environ mutation) ---

def test_backfill_imports_provider_key_when_zero_present(tmp_path):
    _write(tmp_path / "config" / ".env.production", "OPENROUTER_API_KEY=sk-or-x\n")
    env = {}
    bootstrap._backfill_provider_keys(config_dir=str(tmp_path / "config"), env=env)
    assert env.get("OPENROUTER_API_KEY") == "sk-or-x"


def test_backfill_is_key_only_never_flags(tmp_path):
    _write(
        tmp_path / "config" / ".env.production",
        "OPENAI_API_KEY=sk-x\nROB_LOCAL=1\nSOME_ENABLED=true\nUVICORN_WORKERS=4\n",
    )
    env = {}
    bootstrap._backfill_provider_keys(config_dir=str(tmp_path / "config"), env=env)
    assert env.get("OPENAI_API_KEY") == "sk-x"
    assert "POLYROB_LOCAL" not in env
    assert "SOME_ENABLED" not in env
    assert "UVICORN_WORKERS" not in env


def test_backfill_respects_override_false(tmp_path):
    _write(tmp_path / "config" / ".env.production", "OPENAI_API_KEY=from-file\n")
    env = {"OPENAI_API_KEY": "already-set"}
    bootstrap._backfill_provider_keys(config_dir=str(tmp_path / "config"), env=env)
    assert env["OPENAI_API_KEY"] == "already-set"


def test_backfill_noop_when_a_provider_key_already_present(tmp_path):
    _write(tmp_path / "config" / ".env.production", "OPENAI_API_KEY=from-file\n")
    env = {"ANTHROPIC_API_KEY": "sk-ant-have-one-0123456789"}  # well-formed, usable
    bootstrap._backfill_provider_keys(config_dir=str(tmp_path / "config"), env=env)
    assert "OPENAI_API_KEY" not in env  # gate short-circuits: already have a usable key


def test_backfill_copies_allowlisted_tool_secret(tmp_path):
    _write(
        tmp_path / "config" / ".env.production",
        "OPENAI_API_KEY=sk-x\nANYSITE_JWT=jwt-tok\n",
    )
    env = {}
    bootstrap._backfill_provider_keys(config_dir=str(tmp_path / "config"), env=env)
    assert env.get("ANYSITE_JWT") == "jwt-tok"


def test_backfill_fires_when_only_deepseek_key_present(tmp_path):
    # A DEEPSEEK_API_KEY is not *usable* on its own (direct client disabled), so the
    # backfill must still fire and pull a real provider key from config/.env.*.
    _write(tmp_path / "config" / ".env.production",
           "OPENROUTER_API_KEY=sk-or-real-0123456789abcdef\n")
    env = {"DEEPSEEK_API_KEY": "sk-ds-0123456789abcdef"}  # present but non-initializable
    bootstrap._backfill_provider_keys(config_dir=str(tmp_path / "config"), env=env)
    assert env.get("OPENROUTER_API_KEY") == "sk-or-real-0123456789abcdef"


# --- load_env integration (mutates os.environ → snapshot/restore) ---

@pytest.fixture
def clean_environ(monkeypatch, tmp_path):
    """Run with no provider keys in env, HOME/cwd isolated to tmp, env restored."""
    for k in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
        "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "NVIDIA_API_KEY",
        "CONFIG_ENV", "ENV", "POLYROB_ENV_KEY_BACKFILL",
    ):
        monkeypatch.delenv(k, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    work = tmp_path / "work"
    (work / "config").mkdir(parents=True)
    monkeypatch.chdir(work)
    return work


# W1.2 (HANDOFF-env-system-and-key-subscriptions-2026-08-14): the backfill is
# RETIRED — default OFF, one deprecation release with a one-time WARN naming
# `polyrob config migrate`, then _backfill_provider_keys + the flag get deleted.


def test_load_env_default_does_not_backfill_and_warns_once(clean_environ, monkeypatch, caplog):
    import logging
    _write(clean_environ / "config" / ".env.production", "OPENROUTER_API_KEY=sk-or-z\n")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(bootstrap, "_warned_backfill_retired", False)
    with caplog.at_level(logging.WARNING, logger="core.bootstrap"):
        bootstrap.load_env(local_mode=True)
    assert os.environ.get("OPENROUTER_API_KEY") is None
    warned = [r for r in caplog.records if "polyrob config migrate" in r.getMessage()]
    assert warned, "the retired backfill must name the migrate verb when it WOULD have fired"
    # one-time: a second load_env must stay silent
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="core.bootstrap"):
        bootstrap.load_env(local_mode=True)
    assert not [r for r in caplog.records if "polyrob config migrate" in r.getMessage()]


def test_load_env_no_warn_when_a_usable_key_exists(clean_environ, monkeypatch, caplog):
    import logging
    _write(clean_environ / "config" / ".env.production", "OPENROUTER_API_KEY=sk-or-z\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-have-one-0123456789")
    monkeypatch.setattr(bootstrap, "_warned_backfill_retired", False)
    with caplog.at_level(logging.WARNING, logger="core.bootstrap"):
        bootstrap.load_env(local_mode=True)
    assert not [r for r in caplog.records if "polyrob config migrate" in r.getMessage()]


def test_backfill_opt_in_still_fires(clean_environ, monkeypatch):
    # POLYROB_ENV_KEY_BACKFILL=1 keeps the legacy behavior for one release.
    _write(clean_environ / "config" / ".env.production", "OPENROUTER_API_KEY=sk-or-z\n")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("POLYROB_ENV_KEY_BACKFILL", "1")
    bootstrap.load_env(local_mode=True)
    assert os.environ.get("OPENROUTER_API_KEY") == "sk-or-z"


def test_server_load_env_does_not_backfill(clean_environ, monkeypatch):
    _write(clean_environ / "config" / ".env.production", "OPENROUTER_API_KEY=sk-or-z\n")
    monkeypatch.setenv("POLYROB_ENV_KEY_BACKFILL", "1")
    bootstrap.load_env(local_mode=False)
    assert os.environ.get("OPENROUTER_API_KEY") is None


def test_backfill_explicitly_off_stays_silent(clean_environ, monkeypatch, caplog):
    # An operator who explicitly opted out gets neither backfill NOR the nag.
    import logging
    _write(clean_environ / "config" / ".env.production", "OPENROUTER_API_KEY=sk-or-z\n")
    monkeypatch.setenv("POLYROB_ENV_KEY_BACKFILL", "false")
    monkeypatch.setattr(bootstrap, "_warned_backfill_retired", False)
    with caplog.at_level(logging.WARNING, logger="core.bootstrap"):
        bootstrap.load_env(local_mode=True)
    assert os.environ.get("OPENROUTER_API_KEY") is None
    assert not [r for r in caplog.records if "polyrob config migrate" in r.getMessage()]
