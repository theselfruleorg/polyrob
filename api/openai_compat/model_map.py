"""Map an OpenAI `model` string to POLYROB's (provider, model)."""
from core.runtime_config import resolve_runtime_config

def _specs():
    from modules.llm.provider_spec import BUILTIN_SPECS, get_specs
    try:
        return get_specs()
    except Exception:
        return BUILTIN_SPECS  # a broken providers.yaml must not blank the routing tables


def _known_providers() -> tuple:
    """Provider names (and aliases) accepted as a `provider/model` slug head
    (024 seam 11 — derived from the ProviderSpec registry)."""
    names = []
    for s in _specs():
        names.append(s.name)
        names.extend(s.aliases)
    return tuple(names)


def _prefix_to_provider() -> tuple:
    """(model-prefix, provider) routing pairs in registry order, first match wins
    (024 seam 12 — every prefix, built-in or user-declared, comes from the spec's
    ``model_prefixes``)."""
    pairs = []
    seen = set()
    for s in _specs():
        for prefix in s.model_prefixes:
            if prefix not in seen:
                pairs.append((prefix, s.name))
                seen.add(prefix)
    return tuple(pairs)


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
