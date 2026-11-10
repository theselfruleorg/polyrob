"""THE single 'available models' builder. Joins the provider-key oracle x the model registry.

Every surface (CLI model list/picker, WebView capabilities, /v1/models, pricing) is meant
to consume this instead of hand-rolling its own provider/model join.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from modules.llm.profiles import usable_providers_with_credentials
from modules.llm.model_registry import (
    get_model_config_exact, get_registry, _derive_display_name)
from modules.llm.model_types import PROVIDER_BY_NAME
from modules.llm.provider_spec import get_spec
from modules.llm.llm_client_registry import get_default_model


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

    *env* defaults to ``os.environ`` (see ``usable_providers_with_credentials``);
    pass a mapping for testability.

    ``initialized_only`` + ``initialized_providers`` optionally narrow the result to
    providers a caller has already constructed a live client for (e.g. a surface that
    only wants to offer models it can actually serve right now, not just ones with a
    key present). When ``initialized_only`` is False (default), every usable-keyed
    provider's models are listed regardless of ``initialized_providers``.
    """
    providers = usable_providers_with_credentials(env)
    if initialized_only and initialized_providers is not None:
        providers = [p for p in providers if p in initialized_providers]
    reg = get_registry()
    out: list[ModelChoice] = []
    for p in providers:                                  # PROFILES order == preference order
        default = get_default_model(p)
        enum = PROVIDER_BY_NAME.get(p)
        rows = reg.list_models(provider=enum, include_deprecated=False) if enum else []
        listed = {m.name for m in rows}
        for m in rows:
            out.append(ModelChoice(
                provider=p, model=m.name, display_name=m.display_name,
                is_default=(m.name == default),
                context_window=getattr(m, "context_window", 0),
                pricing_hint=format_price_hint(m.pricing),
                supports_vision=getattr(m.capabilities, "supports_vision", False),
                supports_tools=getattr(m.capabilities, "supports_tools", True),
            ))
        spec = get_spec(p)
        if spec is None:
            continue
        for name in dict.fromkeys((*spec.models, *filter(None, [spec.default_model]))):
            if name not in listed:
                out.append(_spec_choice(p, name, spec, is_default=(name == default)))
    return out


def _spec_choice(provider: str, name: str, spec, *, is_default: bool) -> ModelChoice:
    """A model a provider spec declares and the catalog does not list for that provider.
    An alias row gives its limits and capabilities. Its price belongs to the row's own
    provider, so the hint shows only for that provider."""
    row = get_model_config_exact(name)
    own_price = row is not None and row.provider is PROVIDER_BY_NAME.get(provider)
    return ModelChoice(
        provider=provider, model=name,
        display_name=row.display_name if row else _derive_display_name(name),
        is_default=is_default,
        context_window=row.context_window if row else 0,
        pricing_hint=format_price_hint(row.pricing) if own_price else "(pricing n/a)",
        supports_vision=row.capabilities.supports_vision if row else spec.supports_vision,
        supports_tools=row.capabilities.supports_tools if row else spec.supports_native_tools,
    )
