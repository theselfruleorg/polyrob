"""TDD tests for the Task-11 init wizard extensions.

All tests use isolated tmp HOME dirs so the real ~/.polyrob/.env is never touched.
"""
from __future__ import annotations
from pathlib import Path

import pytest
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def _invoke(args, home, monkeypatch, input_text=None):
    """Run init_cmd inside an isolated CWD and HOME."""
    proj = home.parent / "proj"
    proj.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    from cli.commands.init import init_cmd
    runner = CliRunner()
    return runner.invoke(init_cmd, args, input=input_text, catch_exceptions=False)


# ---------------------------------------------------------------------------
# --non-interactive is a byte-identical alias of --no-prompt
# ---------------------------------------------------------------------------

def test_non_interactive_identical_to_no_prompt(tmp_path, monkeypatch):
    home1 = tmp_path / "home1"; home1.mkdir()
    home2 = tmp_path / "home2"; home2.mkdir()

    res1 = _invoke(
        ["--anthropic-key", "sk-a", "--no-prompt"],
        home1, monkeypatch,
    )
    res2 = _invoke(
        ["--anthropic-key", "sk-a", "--non-interactive"],
        home2, monkeypatch,
    )

    assert res1.exit_code == 0, res1.output
    assert res2.exit_code == 0, res2.output
    # Output must be identical (modulo the path which embeds home dir name).
    out1 = res1.output.replace(str(home1), "HOME")
    out2 = res2.output.replace(str(home2), "HOME")
    assert out1 == out2, f"Outputs differ:\n---no-prompt---\n{out1}\n---non-interactive---\n{out2}"

    # Both must write the same env content (ignoring path differences).
    env1 = (home1 / ".polyrob" / ".env").read_text()
    env2 = (home2 / ".polyrob" / ".env").read_text()
    assert env1 == env2


# ---------------------------------------------------------------------------
# --quick skips toolset + template sections
# ---------------------------------------------------------------------------

def test_quick_does_not_prompt_toolset_or_template(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    # All flags supplied so no prompts are needed; piped blank input just in case.
    res = _invoke(
        ["--quick", "--anthropic-key", "sk-q", "--openai-key", "sk-oq",
         "--default-model", "gpt-5"],
        home, monkeypatch,
        input_text="\n",  # extra blank in case any prompt slips through
    )
    assert res.exit_code == 0, res.output
    # Should NOT contain toolset/template section prompts.
    assert "Section 3" not in res.output
    assert "Section 4" not in res.output


def test_quick_writes_keys(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    # Supply all values via flags so no prompts hit.
    res = _invoke(
        ["--quick", "--anthropic-key", "sk-q2", "--openai-key", "sk-oq2",
         "--default-model", "m1"],
        home, monkeypatch,
    )
    assert res.exit_code == 0, res.output
    env = (home / ".polyrob" / ".env").read_text()
    assert "ANTHROPIC_API_KEY=sk-q2" in env
    assert "DEFAULT_MODEL=m1" in env


# ---------------------------------------------------------------------------
# Interactive piped input writes POLYROB_AGENT_TOOLSET
# ---------------------------------------------------------------------------

def test_interactive_piped_input_writes_toolset(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    # Section 1 (6 providers, OpenRouter-first), Section 2 model, Section 3 toolset, Section 4 template.
    piped = "\n".join([
        "",         # openrouter key (blank)
        "sk-ant",   # anthropic key
        "",         # openai key (blank)
        "",         # gemini key (blank)
        "",         # nvidia key (blank)
        "",         # deepseek key (blank)
        "",         # model (blank)
        "research", # toolset
        "research", # template
        "",         # owner pairing: instance id (default rob)
        "",         # owner pairing: owner user id (default rob)
        "",         # 5/5 guardrails: local mode (default No)
        "",         # 5/5 guardrails: autonomy budget (blank = skip)
        "",         # 5/5 guardrails: approval preset (default No)
        "",         # 5/5 guardrails: digest channel (blank = off)
        "n",        # optional wallet opt-in (Task 7, default No)
    ]) + "\n"
    res = _invoke([], home, monkeypatch, input_text=piped)
    assert res.exit_code == 0, res.output
    env = (home / ".polyrob" / ".env").read_text()
    assert "POLYROB_AGENT_TOOLSET=research" in env


def test_interactive_piped_default_toolset(tmp_path, monkeypatch):
    """Accepting defaults (all enter) must still write toolset."""
    home = _make_home(tmp_path)
    # 16 prompts on the all-defaults interactive path: 6 provider keys (OpenRouter-first)
    # + model + toolset + template + owner-pairing (instance id + owner id)
    # + Section 6/6 guardrails (local mode + budget + approval preset + digest)
    # + the wallet opt-in confirm (Task 7, blank = default No). (+1 safety.)
    piped = "\n" * 17  # all blanks / defaults
    res = _invoke([], home, monkeypatch, input_text=piped)
    assert res.exit_code == 0, res.output
    env = (home / ".polyrob" / ".env").read_text()
    assert "POLYROB_AGENT_TOOLSET=" in env


# ---------------------------------------------------------------------------
# Provider is inferred from the chosen model and written alongside DEFAULT_MODEL
# (regression: a model-only pin is DROPPED by resolve_runtime_config, which only
# honors the pin when pinned_provider is truthy).
# ---------------------------------------------------------------------------

def _read_env(home: Path) -> dict:
    text = (home / ".polyrob" / ".env").read_text()
    return {
        k.strip(): v.strip()
        for k, v in (ln.split("=", 1) for ln in text.splitlines() if "=" in ln)
    }


def test_default_provider_inferred_from_model_non_interactive(tmp_path, monkeypatch):
    """--default-model of a KNOWN model writes BOTH DEFAULT_PROVIDER + DEFAULT_MODEL,
    and resolve_runtime_config then honors the pin (does not drop it)."""
    home = _make_home(tmp_path)
    res = _invoke(
        ["--anthropic-key", "sk-a", "--default-model", "claude-sonnet-4-5", "--no-prompt"],
        home, monkeypatch,
    )
    assert res.exit_code == 0, res.output
    env = _read_env(home)
    assert env.get("DEFAULT_MODEL") == "claude-sonnet-4-5"
    assert env.get("DEFAULT_PROVIDER") == "anthropic", env

    # The pin is now honored end-to-end (before the fix DEFAULT_PROVIDER was empty
    # → pinned_provider falsy → resolve_runtime_config dropped the model-only pin).
    from core.runtime_config import resolve_runtime_config
    provider, model = resolve_runtime_config(
        None, None, env={},
        pinned_provider=env.get("DEFAULT_PROVIDER"),
        pinned_model=env.get("DEFAULT_MODEL"),
    )
    assert (provider, model) == ("anthropic", "claude-sonnet-4-5")


def test_default_provider_inferred_interactive(tmp_path, monkeypatch):
    """Picking a known model at the interactive Section-2 prompt infers its provider."""
    home = _make_home(tmp_path)
    piped = "\n".join([
        "",           # openrouter key (blank)
        "",           # anthropic key (blank)
        "sk-oai",     # openai key
        "",           # gemini key (blank)
        "",           # nvidia key (blank)
        "",           # deepseek key (blank)
        "gpt-5.1",    # model → owned by openai
        "",           # toolset (default)
        "",           # template (default)
        "",           # owner pairing: instance id (default rob)
        "",           # owner pairing: owner user id (default rob)
        "",           # 5/5 guardrails: local mode (default No)
        "",           # 5/5 guardrails: autonomy budget (blank = skip)
        "",           # 5/5 guardrails: approval preset (default No)
        "",           # 5/5 guardrails: digest channel (blank = off)
        "n",          # optional wallet opt-in (Task 7, default No)
    ]) + "\n"
    res = _invoke([], home, monkeypatch, input_text=piped)
    assert res.exit_code == 0, res.output
    env = _read_env(home)
    assert env.get("DEFAULT_MODEL") == "gpt-5.1"
    assert env.get("DEFAULT_PROVIDER") == "openai", env


def test_explicit_provider_flag_wins_over_inference(tmp_path, monkeypatch):
    """An explicit --default-provider is never overridden by model inference."""
    home = _make_home(tmp_path)
    res = _invoke(
        ["--default-model", "gpt-5.1", "--default-provider", "openrouter", "--no-prompt"],
        home, monkeypatch,
    )
    assert res.exit_code == 0, res.output
    env = _read_env(home)
    assert env.get("DEFAULT_MODEL") == "gpt-5.1"
    assert env.get("DEFAULT_PROVIDER") == "openrouter", env


def test_unknown_model_no_prompt_leaves_provider_empty(tmp_path, monkeypatch):
    """A custom/unknown model in --no-prompt mode writes no DEFAULT_PROVIDER (no regression)."""
    home = _make_home(tmp_path)
    res = _invoke(
        ["--anthropic-key", "sk-a", "--default-model", "my/custom-model", "--no-prompt"],
        home, monkeypatch,
    )
    assert res.exit_code == 0, res.output
    env = _read_env(home)
    assert env.get("DEFAULT_MODEL") == "my/custom-model"
    assert "DEFAULT_PROVIDER" not in env, env


# ---------------------------------------------------------------------------
# --template flag pre-fills toolset
# ---------------------------------------------------------------------------

def test_template_flag_prefills_toolset(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    res = _invoke(
        ["--template", "coding", "--no-prompt"],
        home, monkeypatch,
    )
    assert res.exit_code == 0, res.output
    env = (home / ".polyrob" / ".env").read_text()
    assert "POLYROB_AGENT_TOOLSET=coding" in env
    assert "POLYROB_PERSONA=coding" in env


def test_template_unknown_falls_back_to_general(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    res = _invoke(
        ["--template", "does_not_exist", "--no-prompt"],
        home, monkeypatch,
    )
    assert res.exit_code == 0, res.output
    env = (home / ".polyrob" / ".env").read_text()
    # general template uses "default" toolset
    assert "POLYROB_AGENT_TOOLSET=default" in env


# ---------------------------------------------------------------------------
# Corrupted / locked existing env doesn't crash the wizard
# ---------------------------------------------------------------------------

def test_corrupted_env_does_not_crash(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    rob_dir = home / ".polyrob"
    rob_dir.mkdir(parents=True)
    env_path = rob_dir / ".env"
    # Write garbage (non-utf8-like but valid bytes as text)
    env_path.write_text("NOT=VALID\x00garbage\nFOO=BAR\n")
    env_path.chmod(0o600)

    res = _invoke(
        ["--anthropic-key", "sk-c", "--no-prompt"],
        home, monkeypatch,
    )
    # Must not raise / crash — exit_code 0 is ideal; at worst a warning.
    # The catch_exceptions=False in _invoke means any exception propagates,
    # so if we reach here we're good.
    assert res.exit_code == 0 or "Warning" in res.output


def test_existing_env_preserved_on_write(tmp_path, monkeypatch):
    """Pre-existing keys in ~/.polyrob/.env are preserved after init."""
    home = _make_home(tmp_path)
    rob_dir = home / ".polyrob"
    rob_dir.mkdir(parents=True)
    env_path = rob_dir / ".env"
    env_path.write_text("EXISTING_KEY=keep_me\nOTHER=value\n")
    env_path.chmod(0o600)

    res = _invoke(
        ["--anthropic-key", "sk-new", "--no-prompt"],
        home, monkeypatch,
    )
    assert res.exit_code == 0, res.output
    env = env_path.read_text()
    assert "EXISTING_KEY=keep_me" in env
    assert "ANTHROPIC_API_KEY=sk-new" in env


# ---------------------------------------------------------------------------
# Regression: existing test_init.py scenario still passes
# ---------------------------------------------------------------------------

def test_init_writes_files_regression(tmp_path, monkeypatch):
    home = _make_home(tmp_path)
    proj = tmp_path / "proj"; proj.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(proj)
    from cli.commands.init import init_cmd
    res = CliRunner().invoke(
        init_cmd,
        ["--anthropic-key", "sk-x", "--default-model", "claude-opus-4-8", "--no-prompt"],
    )
    assert res.exit_code == 0
    env = (home / ".polyrob" / ".env").read_text()
    assert "ANTHROPIC_API_KEY=sk-x" in env
    assert "DEFAULT_MODEL=claude-opus-4-8" in env
    assert (home / ".polyrob" / ".env").stat().st_mode & 0o777 == 0o600
    assert (proj / ".polyrob" / "sessions").is_dir()
    assert ".polyrob/" in (proj / ".gitignore").read_text()
