"""Refuse unknown model prices before spending on a billed request."""
import math


def require_billable_model(model: str, provider: str) -> None:
    from core.billing_context import is_billed
    if not is_billed():
        return
    from modules.llm.model_registry import get_model_config_exact
    from modules.llm.provider_spec import is_flat_rate
    if is_flat_rate(provider):
        return  # Explicit subscription seats have zero marginal token cost.
    config = get_model_config_exact(model)
    pricing = getattr(config, "pricing", None)
    try:
        rates = (float(pricing.input_price), float(pricing.output_price))
        valid = all(math.isfinite(rate) and rate >= 0 for rate in rates)
    except (AttributeError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Billed requests require an explicitly priced model; register its pricing before use")


def check_adapter(adapter) -> None:
    from core.llm_auth.inference import require_credential_access
    require_credential_access(getattr(adapter, '_client', None))
    from core.billing_context import is_billed
    if is_billed():
        from modules.llm.aux_metering import _llm_identity
        model, provider = _llm_identity(adapter)
        require_billable_model(model, provider)


def inference_sdk(client):
    """Authorize the actual cached credential and avoid hidden paid retries.

    with_options creates a request-local SDK view; shared clients are unchanged.
    The agent's explicit retry creates another reservation and preserves the
    unknown first attempt, instead of silently spending twice against one hold.
    """
    from core.llm_auth.inference import require_credential_access
    from core.billing_context import is_billed, is_prepaid
    require_credential_access(client)
    sdk = client._client
    if is_billed() and not is_prepaid():
        return sdk.with_options(max_retries=0)
    return sdk


def billed_generation_params(adapter, params):
    from core.billing_context import is_billed
    if not is_billed():
        return params
    from modules.llm.aux_metering import _llm_identity
    from modules.llm.model_registry import get_model_config_exact
    model, _provider = _llm_identity(adapter)
    config = get_model_config_exact(model)
    if config is None:
        return params  # Explicit flat-rate seats may have no per-token catalog.
    ceiling = config.max_completion_tokens
    requested = params.get('max_tokens', ceiling)
    if isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0:
        raise ValueError('Billed inference requires a positive output token limit')
    params['max_tokens'] = min(requested, ceiling)
    return params


def billed_google_options():
    from core.billing_context import is_billed, is_prepaid
    return {'request_options': {'retry': None}} if is_billed() and not is_prepaid() else {}
