"""The rules of ``translate_llm_error``: which exception class a status or a text means.

Pure and with no import of the provider layer. The caller reads the two credit facts,
because their markers live in ``core.credit_sentinel``.
"""
import re
from typing import Any

from core.exceptions import (
    LLMAuthenticationError,
    LLMConnectionError,
    LLMContextLengthError,
    LLMError,
    LLMInvalidRequestError,
    LLMModerationError,
    LLMPermanentError,
    LLMRateLimitError,
    LLMRefusalError,
)


def filtered_error(label: str, model: Any, reason: Any) -> LLMRefusalError:
    """The typed error of an answer a content or safety filter stopped: no retry, no other
    provider in its place, and the owner is told."""
    return LLMRefusalError(f"{label}: the content filter stopped model {model}",
                           category=reason or None, kind="filtered")


def _codes(*codes: int) -> str:
    """Word-bounded HTTP codes for the text path. A count or a price is not a code:
    "130402 tokens", "429 tokens" and "$0.429" do not match."""
    joined = "|".join(str(c) for c in codes)
    return rf"(?<![.$\d])\b(?:{joined})\b(?!\s*(?:tokens?|chars?|characters|bytes)\b)"


_CONTEXT_RE = re.compile(
    r"context[_ ]length|maximum context|context window|token limit|maximum tokens"
    r"|too many tokens(?! per)|exceeds? the maximum number of tokens|exceed context limit"
    r"|\b(?:prompt|input|messages?|request|content)\b[^.]{0,40}\btoo long\b"
)
DIGITS_RE = re.compile(r"\d+")
ACCOUNT_RE = re.compile(r"account_deactivated|\bsuspended\b")
_CODE_402_RE = re.compile(_codes(402))
NOT_FOUND_RE = re.compile(rf"{_codes(404)}|not_found|model[_ ]not[_ ]found")
#: OpenRouter has the model and no upstream may serve this request now. Another route may.
_NO_ROUTE_RE = re.compile(r"\bno endpoints found\b|\bno allowed providers\b")
_RATE_RE = re.compile(
    r"\brate[_ ]?limit|too many requests|\bquota\b|resource exhausted|\blimit reached\b"
    rf"|{_codes(429)}"
)
_AUTH_RE = re.compile(
    r"\bauthenticat|\bunauthori[sz]ed\b|\bforbidden\b|\bauth\b|api[_ ]?key|invalid key"
    rf"|{_codes(401, 403)}"
)
#: Google answers a bad key with a 400, so the status alone does not say it.
_KEY_RE = re.compile(r"api[_ ]?key|invalid key")
_INVALID_RE = re.compile(rf"\binvalid|\bbad request\b|\bmalformed\b|{_codes(400)}")
_CONNECTION_RE = re.compile(
    r"\bconnection\b|\bconnect\b|\bnetwork\b|time[d ]?\s?out|\bunreachable\b|\bbad gateway\b"
    r"|\bservice unavailable\b|\binternal server error\b|\boverloaded\b"
    rf"|{_codes(500, 502, 503, 504, 520, 521, 522, 523, 524, 529)}"
)
_MODERATION_RE = re.compile(r"\bmoderation\b|\bflagged\b")


class ProviderBodyError(Exception):
    """A provider error delivered in the body of an HTTP 200."""

    def __init__(self, body_error: Any):
        code = body_error.get("code") if isinstance(body_error, dict) else None
        text = str(body_error.get("message") or body_error) if isinstance(body_error, dict) else str(body_error)
        self.code = code
        super().__init__(f"{code}: {text}" if code else text)


def class_for_status(status: int, low: str, *, no_credit: bool, transient_budget: bool) -> type:
    if status == 402:
        return LLMRateLimitError if transient_budget else LLMPermanentError
    if status >= 500:
        return LLMConnectionError  # a 5xx body is not read: it says nothing of the account
    if status == 413 or (status in (400, 422) and _CONTEXT_RE.search(low)):
        return LLMContextLengthError
    if no_credit:
        return LLMPermanentError
    if _NO_ROUTE_RE.search(low):
        return LLMConnectionError
    if status == 403 and _MODERATION_RE.search(low):
        return LLMModerationError  # a refusal: never another provider in its place
    if status in (401, 403) or (status in (400, 422) and _KEY_RE.search(low)):
        return LLMAuthenticationError
    if status == 404:
        return LLMPermanentError
    if status in (408, 409, 425):
        return LLMConnectionError
    if status == 429:
        return LLMRateLimitError
    return LLMInvalidRequestError


def class_for_text(low: str, *, no_credit: bool, transient_budget: bool) -> type:
    if _CONTEXT_RE.search(low):
        return LLMContextLengthError
    if transient_budget:
        return LLMRateLimitError
    if no_credit or _CODE_402_RE.search(low):
        return LLMPermanentError
    if _NO_ROUTE_RE.search(low):
        return LLMConnectionError
    if NOT_FOUND_RE.search(low):
        return LLMPermanentError
    if _RATE_RE.search(low):
        return LLMRateLimitError
    if _AUTH_RE.search(low):
        return LLMAuthenticationError
    if _INVALID_RE.search(low):
        return LLMInvalidRequestError
    if _CONNECTION_RE.search(low):
        return LLMConnectionError
    return LLMError
