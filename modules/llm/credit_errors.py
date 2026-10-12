"""A provider that refuses for lack of CREDIT is not rate-limiting you.

Prod 2026-10-09: z.ai answered every call with HTTP 429 ``type=rate_limit_error``
``code=1113 "Insufficient balance or no resource package. Please recharge."``.
The SDK raised ``anthropic.RateLimitError``; the client wrapped it as a generic
``ServiceError``/``LLMError``; the step loop read "429" and retried it four times
as a rate limit (each try = two call shapes x three HTTP attempts) before the
adapter's classifier finally called it PERMANENT. 102 log lines, no money, no
answer.

:func:`provider_credit_error` turns such a refusal into ONE
``LLMPermanentError`` whose text names the provider and says "no balance", so
every layer above (step-loop retry bucket, error_recovery billing failover, the
credit sentinel) reads it as billing on the first try. The marker set is the
credit sentinel's (``core.credit_sentinel.looks_like_credit_death``) — one
definition of "credit death", not a second.
"""
import re
from typing import Optional

from core.exceptions import LLMPermanentError

#: z.ai/GLM business codes that mean "the account cannot pay": 1113 = insufficient
#: balance / no resource package; 1310 = weekly/monthly plan limit exhausted.
_CODE_RE = re.compile(r"""['"]code['"]\s*:\s*['"]?(\d{3,5})|\[(\d{4})\]""")


def _provider_code(text: str) -> str:
    m = _CODE_RE.search(text or "")
    return (m.group(1) or m.group(2)) if m else ""


def provider_credit_error(provider_label: str, exc: BaseException) -> Optional[LLMPermanentError]:
    """``LLMPermanentError`` naming the provider and "no balance" — or None when
    ``exc`` is not a credit refusal (the caller then keeps its own wrapping)."""
    if isinstance(exc, LLMPermanentError) and "NO BALANCE" in str(exc):
        return exc
    text = str(exc)
    try:
        from core.credit_sentinel import looks_like_credit_death
        if not looks_like_credit_death(text):
            return None
    except Exception:
        return None
    code = _provider_code(text)
    code_part = f" (provider code {code})" if code else ""
    return LLMPermanentError(
        f"{provider_label}: NO BALANCE — the provider refused for lack of credit"
        f"{code_part}; this is billing, not a rate limit, so it is not retried. "
        f"Recharge the {provider_label} account or switch provider. "
        f"Provider said: {text[:300]}")


def raise_if_credit_refusal(provider_label: str, exc: BaseException, log=None) -> None:
    """Raise the named ``LLMPermanentError`` (from ``exc``) for a credit refusal;
    return quietly otherwise so the caller keeps its own wrapping."""
    err = provider_credit_error(provider_label, exc)
    if err is not None:
        if log is not None:
            log.warning(str(err)[:300])
        raise err from exc


async def no_retry_on_credit_refusal(response) -> None:
    """httpx response hook: a 429 whose body is a credit refusal (z.ai 1113)
    gets ``x-should-retry: false``, which the Anthropic/OpenAI SDKs obey, so one
    refusal is ONE HTTP call instead of three (``max_retries`` default 2). A
    real rate limit keeps the SDK's backoff. Fail-open."""
    try:
        if response.status_code != 429:
            return
        await response.aread()
        from core.credit_sentinel import looks_like_credit_death
        if looks_like_credit_death(response.text or ""):
            response.headers["x-should-retry"] = "false"
    except Exception:
        return
