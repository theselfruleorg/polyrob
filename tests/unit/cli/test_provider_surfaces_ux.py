"""CLI provider-surface fixes from the UX assessment (2026-08-07): Q5/Q7/Q8/Q15.

- `polyrob model list` status derives from the doctor oracles (present/
  malformed/missing + "no key needed"), never AgentConfig pydantic fields that
  only exist for built-ins.
- `polyrob doctor` renders a providers-file block (loaded/rejected + reasons),
  honest keyless rows, and no phantom resolved-provider on a keyless box.
- `polyrob init` never prompts for an env key a keyless row doesn't have.
"""
import textwrap

import pytest
from click.testing import CliRunner


OLLAMA_ZAI_YAML = textwrap.dedent("""\
    providers:
      ollama:
        base_url: http://127.0.0.1:11434/v1
        auth_type: none
        transport: chat_completions
        default_model: qwen3-coder:30b
        models: [qwen3-coder:30b]
      zai-coding:
        base_url: https://api.z.ai/api/anthropic
        auth_type: api_key
        env_key: ZAI_API_KEY
        transport: anthropic_messages
        models: [glm-5]
        default_model: glm-5
""")


@pytest.fixture
def user_providers(tmp_path, monkeypatch):
    path = tmp_path / "providers.yaml"
    path.write_text(OLLAMA_ZAI_YAML)
    monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", str(path))
    monkeypatch.setenv("LLM_PROVIDER_REGISTRY", "true")
    from modules.llm.provider_spec import reset_provider_registry_cache
    reset_provider_registry_cache()
    yield path
    reset_provider_registry_cache()


@pytest.fixture
def no_builtin_keys(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
              "OPENROUTER_API_KEY", "NVIDIA_API_KEY", "DEEPSEEK_API_KEY",
              "ZAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)


# --- Q5: model list status column ---------------------------------------------

def _run_model_list(monkeypatch):
    monkeypatch.setattr("core.bootstrap.load_env", lambda **k: None)
    from cli.commands.model import model_list
    res = CliRunner().invoke(model_list, [], color=False)
    assert res.exit_code == 0, f"{res.output!r} exc={res.exception!r}"
    return res.output


def test_model_list_keyed_user_provider_shows_present(user_providers, no_builtin_keys, monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "zk-0123456789abcdef0123456789")
    out = _run_model_list(monkeypatch)
    row = next(l for l in out.splitlines() if l.startswith("zai-coding"))
    assert "present" in row
    assert "no key" not in row


def test_model_list_keyless_row_says_no_key_needed(user_providers, no_builtin_keys, monkeypatch):
    out = _run_model_list(monkeypatch)
    row = next(l for l in out.splitlines() if l.startswith("ollama"))
    assert "no key needed" in row
    # the footer must not deny the usable keyless provider (the model join
    # doesn't list user providers until L1.5 — say that, not "no usable key")
    assert "No usable provider key found" not in out
    assert "ollama" in out.split("Native", 1)[1]


def test_model_list_malformed_key_not_ready(user_providers, no_builtin_keys, monkeypatch):
    """A placeholder key must not render as usable on the same screen whose
    footer says no usable key exists (the ready-vs-footer contradiction)."""
    monkeypatch.setenv("OPENAI_API_KEY", "your_openai_key")
    out = _run_model_list(monkeypatch)
    row = next(l for l in out.splitlines() if l.startswith("openai"))
    assert "malformed" in row
    assert "ready" not in row


# --- Q7 + Q15: doctor block ----------------------------------------------------

def test_doctor_renders_providers_file_block(tmp_path, monkeypatch, no_builtin_keys):
    path = tmp_path / "providers.yaml"
    path.write_text(textwrap.dedent("""\
        providers:
          good:
            base_url: http://127.0.0.1:9999/v1
            transport: chat_completions
            models: [m1]
          badrow:
            base_url: http://127.0.0.1:9999/v1
            transport: teleport
    """))
    monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", str(path))
    monkeypatch.setenv("LLM_PROVIDER_REGISTRY", "true")
    from modules.llm.provider_spec import reset_provider_registry_cache
    reset_provider_registry_cache()
    try:
        from cli.commands.doctor import doctor_report
        report = "\n".join(doctor_report({}))
        assert f"providers file: {path}" in report
        assert "1 loaded, 1 rejected" in report
        assert "! badrow:" in report
    finally:
        reset_provider_registry_cache()


def test_doctor_no_providers_file_block_when_absent(tmp_path, monkeypatch, no_builtin_keys):
    monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", str(tmp_path / "absent.yaml"))
    from modules.llm.provider_spec import reset_provider_registry_cache
    reset_provider_registry_cache()
    try:
        from cli.commands.doctor import doctor_report
        assert "providers file:" not in "\n".join(doctor_report({}))
    finally:
        reset_provider_registry_cache()


def test_doctor_keyless_row_is_not_missing(user_providers, no_builtin_keys):
    from cli.commands.doctor import doctor_report
    report = "\n".join(doctor_report({}))
    assert "ollama: no key needed" in report
    # a usable keyless provider means the box is NOT keyless-broken
    assert "no provider API key found" not in report
    # and the resolver reports it instead of the gemini last-resort phantom
    assert "resolved provider/model: ollama" in report


def test_doctor_zero_keys_resolved_line_is_honest(monkeypatch, no_builtin_keys):
    """With nothing usable, doctor must not display a resolved provider that
    cannot actually serve (the gemini last-resort leak)."""
    monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", "")
    from modules.llm.provider_spec import reset_provider_registry_cache
    reset_provider_registry_cache()
    try:
        from cli.commands.doctor import doctor_report
        report = "\n".join(doctor_report({}))
        assert "resolved provider/model: (none" in report
        assert "resolved provider/model: gemini" not in report
    finally:
        reset_provider_registry_cache()


# --- Q8: init never prompts for a keyless row ---------------------------------

def test_init_key_prompts_skip_keyless_rows(user_providers, monkeypatch):
    prompts = []

    def fake_prompt(text, **kwargs):
        prompts.append(text)
        return ""

    monkeypatch.setattr("click.prompt", fake_prompt)
    from cli.commands.init import _prompt_provider_keys
    _prompt_provider_keys({})
    joined = "\n".join(prompts)
    assert "ZAI_API_KEY" not in joined  # sanity: prompts are display-name-led
    assert "zai-coding" in joined       # keyed user row IS prompted
    assert "ollama" not in joined       # keyless row is NOT
