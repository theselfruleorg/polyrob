"""106 K3 + K8: the cheap aux model on both DeepSeek routes, and a fallback token that
names a vendor slug is one model id, not a provider."""
import pytest

from agents.task.constants import parse_provider_model, resolve_aux_chain, resolve_aux_model
from modules.llm.model_registry import get_model_config_exact


@pytest.fixture(autouse=True)
def _clean_aux_env(monkeypatch):
    for name in ("AUX_MODEL_COMPACTION", "AUX_MODEL_JUDGE", "AUX_MODEL_REFLECTION",
                 "AUX_PROVIDER_COMPACTION", "AUX_PROVIDER_JUDGE", "AUX_FALLBACK_JUDGE",
                 "AUX_FALLBACK_COMPACTION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AUX_AUTO", "true")


def test_openrouter_pro_gets_flash_for_aux():
    assert resolve_aux_model("judge", "openrouter", "deepseek/deepseek-v4-pro") \
        == "deepseek/deepseek-v4.1-flash"


def test_openrouter_flash_stays_on_itself():
    assert resolve_aux_model("judge", "openrouter", "deepseek/deepseek-v4.1-flash") is None


def test_native_deepseek_gets_flash_for_aux_on_the_native_route():
    chain = resolve_aux_chain("compaction", "deepseek", "deepseek-v4-pro")
    assert chain[0] == {"model": "deepseek-flash", "provider": "deepseek"}


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini", "google", "deepseek",
                                      "zai", "zai-coding"])
def test_every_cheap_map_row_has_an_exact_catalog_row(provider):
    from modules.llm.provider_spec import get_spec
    assert get_model_config_exact(get_spec(provider).aux_model) is not None


def test_a_vendor_slug_is_one_model_id():
    assert parse_provider_model("z-ai/glm-5.3-flash") \
        == {"model": "z-ai/glm-5.3-flash", "provider": None}


def test_a_token_with_its_own_row_is_one_model_id():
    assert parse_provider_model("deepseek/deepseek-v4-pro") \
        == {"model": "deepseek/deepseek-v4-pro", "provider": None}


def test_a_known_provider_head_still_splits():
    assert parse_provider_model("openrouter/z-ai/glm-5.3-flash") \
        == {"model": "z-ai/glm-5.3-flash", "provider": "openrouter"}
    assert parse_provider_model("deepseek/deepseek-flash") \
        == {"model": "deepseek-flash", "provider": "deepseek"}


def test_the_fallback_chain_keeps_the_slug_whole(monkeypatch):
    monkeypatch.setenv("AUX_MODEL_JUDGE", "deepseek/deepseek-v4.1-flash")
    monkeypatch.setenv("AUX_FALLBACK_JUDGE", "z-ai/glm-5.3-flash,bare-model")
    chain = resolve_aux_chain("judge", "openrouter", "deepseek/deepseek-v4-pro")
    assert chain[1:] == [{"model": "z-ai/glm-5.3-flash", "provider": None},
                         {"model": "bare-model", "provider": None}]
