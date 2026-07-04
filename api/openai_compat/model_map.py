"""Map an OpenAI `model` string to POLYROB's (provider, model)."""
from core.runtime_config import resolve_runtime_config

_KNOWN_PROVIDERS = ("openai", "anthropic", "gemini", "deepseek", "openrouter", "nvidia")
_PREFIX_TO_PROVIDER = (("gpt", "openai"), ("o1", "openai"), ("o3", "openai"),
                       ("claude", "anthropic"), ("gemini", "gemini"),
                       ("deepseek", "deepseek"), ("kimi", "nvidia"))


def map_model(openai_model: str) -> tuple[str, str]:
    """Return (provider, model). A `provider/model` slug wins (split on first '/');
    else a known bare-model prefix; else the env-resolved default provider."""
    s = (openai_model or "").strip()
    if "/" in s:
        head, tail = s.split("/", 1)
        if head.lower() in _KNOWN_PROVIDERS and tail:
            return head.lower(), tail
    low = s.lower()
    for prefix, provider in _PREFIX_TO_PROVIDER:
        if low.startswith(prefix):
            return provider, s
    # Server path: no ~/.polyrob/cli.json read (cli_store_default=None) — just the
    # env-resolved first-keyed provider.
    provider, _ = resolve_runtime_config(None, None)
    return provider, s
