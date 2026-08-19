"""Map an OpenAI `model` string to POLYROB's (provider, model)."""
from core.runtime_config import resolve_runtime_config

# Kill-switch path (LLM_PROVIDER_REGISTRY=off) — the live sets are derived from
# the ProviderSpec registry by _known_providers()/_prefix_to_provider() below
# (024 seams 11/12), so a providers.yaml provider routes instead of silently
# falling through to the env-default provider.
_KNOWN_PROVIDERS = ("openai", "anthropic", "gemini", "deepseek", "openrouter", "nvidia")
_PREFIX_TO_PROVIDER = (("gpt", "openai"), ("o1", "openai"), ("o3", "openai"),
                       ("claude", "anthropic"), ("gemini", "gemini"),
                       ("deepseek", "deepseek"), ("kimi", "nvidia"))


def _known_providers() -> tuple:
    """Provider names (and aliases) accepted as a `provider/model` slug head."""
    try:
        from modules.llm.provider_spec import get_specs, provider_registry_enabled
        if provider_registry_enabled():
            names = []
            for s in get_specs():
                names.append(s.name)
                names.extend(s.aliases)
            return tuple(names)
    except Exception:
        pass
    return _KNOWN_PROVIDERS


def _prefix_to_provider() -> tuple:
    """(model-prefix, provider) routing pairs, spec-declared prefixes included.

    Built-in prefixes keep the legacy literal order (first match wins); user
    specs' ``model_prefixes`` append after, in registry order.
    """
    try:
        from modules.llm.provider_spec import get_specs, provider_registry_enabled
        if provider_registry_enabled():
            pairs = list(_PREFIX_TO_PROVIDER)
            seen = {p for p, _ in pairs}
            for s in get_specs():
                for prefix in s.model_prefixes:
                    if prefix not in seen:
                        pairs.append((prefix, s.name))
                        seen.add(prefix)
            return tuple(pairs)
    except Exception:
        pass
    return _PREFIX_TO_PROVIDER


def _provider_owning(model: str) -> str | None:
    """The provider whose registry ``AVAILABLE_MODELS`` contains *model* exactly, or None.

    Registry membership is the SSOT for provider ownership (same mechanism
    ``cli/config_store.py::_provider_for_model`` uses). This catches registered vendor
    slugs whose head is NOT a known-provider prefix — ``z-ai/glm-*`` (openrouter),
    ``moonshotai/kimi-*`` (nvidia), grok/qwen/etc. — which the bare-prefix table below
    would miss and misroute to the env default. Fail-open to None.

    ⚠ Ownership is NO LONGER unambiguous. Once 30+ providers ship, the same
    open-weight id is served by several of them (``glm-5.2`` by ollama-cloud,
    zai and openrouter; ``kimi-k2.6`` by ollama-cloud, moonshot and nvidia).
    First-declaring-wins would route a request to a provider the caller has no
    credential for — a confusing 401 for a model they can actually serve. So
    among the providers declaring the model, prefer one that is actually
    USABLE, and only fall back to declaration order when none is."""
    try:
        from modules.llm.llm_client_registry import AVAILABLE_MODELS
    except Exception:
        return None
    owners = [provider for provider, models in AVAILABLE_MODELS.items() if model in models]
    if not owners:
        return None
    if len(owners) == 1:
        return owners[0]
    try:
        from modules.llm.profiles import usable_providers_with_credentials
        usable = usable_providers_with_credentials()
        for provider in owners:                    # declaration order among usable
            if provider in usable:
                return provider
    except Exception:
        pass
    return owners[0]


def map_model(openai_model: str) -> tuple[str, str]:
    """Return (provider, model). A `provider/model` slug wins (split on first '/');
    else exact registry membership (the SSOT); else a known bare-model prefix; else the
    env-resolved default provider."""
    s = (openai_model or "").strip()
    if "/" in s:
        head, tail = s.split("/", 1)
        if head.lower() in _known_providers() and tail:
            head_low = head.lower()
            # Resolve an alias to its canonical provider name.
            try:
                from modules.llm.provider_spec import get_spec
                spec = get_spec(head_low)
                if spec is not None:
                    head_low = spec.name
            except Exception:
                pass
            return head_low, tail
    owner = _provider_owning(s)
    if owner:
        return owner, s
    low = s.lower()
    for prefix, provider in _prefix_to_provider():
        if low.startswith(prefix):
            return provider, s
    # Server path: no ~/.polyrob/cli.json read (cli_store_default=None) — honor the
    # operator provider pin (CHAT_PROVIDER/DEFAULT_PROVIDER), else the env-resolved
    # first-keyed provider.
    from core.runtime_config import resolve_default_provider
    provider, _ = resolve_default_provider()
    return provider, s
