"""THE single 'available models' builder. Joins the provider-key oracle x the model registry.

Every surface (CLI model list/picker, WebView capabilities, /v1/models, pricing) is meant
to consume this instead of hand-rolling its own provider/model join (there were three
divergent builders before this module; later tasks repoint them here).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from modules.llm.profiles import usable_providers_with_keys, providers_with_keys
from modules.llm.model_registry import get_registry, PROVIDER_CANONICAL_NAMES, ModelProvider
from modules.llm.llm_client_registry import get_default_model

# canonical string -> enum (handles gemini/google). Build once.
#
# NOTE: PROVIDER_CANONICAL_NAMES is keyed by the ModelProvider ENUM MEMBER (e.g.
# ModelProvider.GOOGLE), not by its .value string ("google") -- so the lookup below
# must key off `_e` itself, not `_e.value`. Getting this backwards silently drops the
# "gemini" -> ModelProvider.GOOGLE mapping (only "google" would resolve), which would
# make `available_models()` return an empty list for every Gemini-keyed env even though
# `usable_providers_with_keys()` correctly reports "gemini" as usable.
_STR_TO_ENUM = {}
for _e in ModelProvider:
    _STR_TO_ENUM[PROVIDER_CANONICAL_NAMES.get(_e, _e.value)] = _e
    _STR_TO_ENUM[_e.value] = _e


@dataclass(frozen=True)
class ModelChoice:
    provider: str
    model: str
    display_name: str
    is_default: bool
    context_window: int
    pricing_hint: str
    supports_vision: bool
    supports_tools: bool


def format_price_hint(pricing) -> str:
    """Render a short human-readable price hint, e.g. '$3 / $15 per 1M (indicative)'."""
    try:
        return f"${pricing.input_price:g} / ${pricing.output_price:g} per 1M (indicative)"
    except Exception:
        return "(pricing n/a)"


def format_context(n: int) -> str:
    """Render a context-window size compactly, e.g. 128000 -> '128K', 1_000_000 -> '1M'."""
    if not n:
        return ""
    return f"{n // 1000}K" if n < 1_000_000 else f"{n / 1_000_000:g}M"


def available_models(env=None, *, initialized_only: bool = False,
                      initialized_providers: Optional[set] = None) -> list[ModelChoice]:
    """The one join: usable-key providers (in PROFILES preference order) x their models.

    *env* defaults to ``os.environ`` (see ``usable_providers_with_keys``/
    ``providers_with_keys``); pass a mapping for testability.

    ``initialized_only`` + ``initialized_providers`` optionally narrow the result to
    providers a caller has already constructed a live client for (e.g. a surface that
    only wants to offer models it can actually serve right now, not just ones with a
    key present). When ``initialized_only`` is False (default), every usable-keyed
    provider's models are listed regardless of ``initialized_providers``.
    """
    providers = usable_providers_with_keys(env)          # excludes deepseek (initializable=False)
    if initialized_only and initialized_providers is not None:
        providers = [p for p in providers if p in initialized_providers]
    reg = get_registry()
    out: list[ModelChoice] = []
    for p in providers:                                  # PROFILES order == preference order
        default = get_default_model(p)
        enum = _STR_TO_ENUM.get(p)
        if enum is None:
            continue
        for m in reg.list_models(provider=enum, include_deprecated=False):
            out.append(ModelChoice(
                provider=p, model=m.name, display_name=m.display_name,
                is_default=(m.name == default),
                context_window=getattr(m, "context_window", 0),
                pricing_hint=format_price_hint(m.pricing),
                supports_vision=getattr(m.capabilities, "supports_vision", False),
                supports_tools=getattr(m.capabilities, "supports_tools", True),
            ))
    return out


def steer_notes(env=None) -> list[str]:
    """Advisory notes for keys present but not directly usable (e.g. DeepSeek).

    A DEEPSEEK_API_KEY alone cannot bootstrap the agent (direct client disabled --
    tool-calling broken), so it never appears in ``available_models``. Surfaces that
    show "your keys" (doctor, CLI init, WebView settings) should render this note so
    the key isn't silently ignored with no explanation.
    """
    notes = []
    present = set(providers_with_keys(env))              # DISPLAY oracle (includes deepseek)
    usable = set(usable_providers_with_keys(env))
    if "deepseek" in present and "deepseek" not in usable:
        notes.append("A DeepSeek key is set but its direct client is disabled. Reach DeepSeek via "
                     "OPENROUTER_API_KEY with model deepseek/deepseek-chat.")
    return notes
