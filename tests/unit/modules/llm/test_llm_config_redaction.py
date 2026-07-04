"""LLM config must never be logged with raw api_key values.

`LLMManager._initialize` used to `logger.info("Full LLM config: {config}")`, which
printed every provider's real api_key to stdout on every container build (visible
on the CLI). The redaction helper masks keys while keeping the diagnostic value.
"""

from modules.llm.llm_manager import _redact_llm_config


def test_redact_masks_api_keys_but_keeps_shape():
    cfg = {
        "openai": {"api_key": "sk-proj-realsecret123"},
        "openrouter": {"api_key": "sk-or-v1-abc", "site_name": "POLYROB"},
        "nvidia": {"api_key": None, "api_url": "https://integrate.example"},
    }
    out = _redact_llm_config(cfg)
    flat = str(out)
    assert "sk-proj-realsecret123" not in flat
    assert "sk-or-v1-abc" not in flat
    # present/absent distinction is retained for diagnostics
    assert out["openai"]["api_key"] == "<set>"
    assert out["nvidia"]["api_key"] == "<missing>"
    # non-secret fields are preserved
    assert out["openrouter"]["site_name"] == "POLYROB"
    assert out["nvidia"]["api_url"] == "https://integrate.example"


def test_redact_does_not_mutate_input():
    cfg = {"openai": {"api_key": "sk-secret"}}
    _redact_llm_config(cfg)
    assert cfg["openai"]["api_key"] == "sk-secret"


def test_redact_handles_empty_and_nondict():
    assert _redact_llm_config({}) == {}
    assert _redact_llm_config(None) == {}
    # a non-dict provider value is passed through untouched (defensive)
    assert _redact_llm_config({"weird": "x"}) == {"weird": "x"}
