"""Map an OpenAI `model` string to POLYROB's (provider, model)."""
from core.runtime_config import resolve_runtime_config

_KNOWN_PROVIDERS = ("openai", "anthropic", "gemini", "deepseek", "openrouter", "nvidia")
_PREFIX_TO_PROVIDER = (("gpt", "openai"), ("o1", "openai"), ("o3", "openai"),
                       ("claude", "anthropic"), ("gemini", "gemini"),
                       ("deepseek", "deepseek"), ("kimi", "nvidia"))


def _provider_owning(model: str) -> str | None:
    """The provider whose registry ``AVAILABLE_MODELS`` contains *model* exactly, or None.

    Registry membership is the SSOT for provider ownership (same mechanism
    ``cli/config_store.py::_provider_for_model`` uses). This catches registered vendor
    slugs whose head is NOT a known-provider prefix — ``z-ai/glm-*`` (openrouter),
    ``moonshotai/kimi-*`` (nvidia), grok/qwen/etc. — which the bare-prefix table below
    would miss and misroute to the env default. No model appears under two providers,
    so the lookup is unambiguous. Fail-open to None."""
    try:
        from modules.llm.llm_client_registry import AVAILABLE_MODELS
    except Exception:
        return None
    for provider, models in AVAILABLE_MODELS.items():
        if model in models:
            return provider
    return None


def map_model(openai_model: str) -> tuple[str, str]:
    """Return (provider, model). A `provider/model` slug wins (split on first '/');
    else exact registry membership (the SSOT); else a known bare-model prefix; else the
    env-resolved default provider."""
    s = (openai_model or "").strip()
    if "/" in s:
        head, tail = s.split("/", 1)
        if head.lower() in _KNOWN_PROVIDERS and tail:
            return head.lower(), tail
    owner = _provider_owning(s)
    if owner:
        return owner, s
    low = s.lower()
    for prefix, provider in _PREFIX_TO_PROVIDER:
        if low.startswith(prefix):
            return provider, s
    # Server path: no ~/.polyrob/cli.json read (cli_store_default=None) — just the
    # env-resolved first-keyed provider.
    provider, _ = resolve_runtime_config(None, None)
    return provider, s
