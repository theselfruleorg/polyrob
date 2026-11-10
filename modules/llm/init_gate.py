"""Per-provider lazy-init gate and call-result health.

A provider is ``ready`` only after its client initialised; a failed attempt
(an SDK/DNS fault, a key that is not set yet) retries after a backoff
``min(base * 2**(n-1), cap)`` instead of latching the provider off until a
restart. One ``asyncio.Lock`` per provider serialises concurrent first calls.

Health reads real call results per provider: an auth / connection / permanent
error from any client of a provider, isolated or shared, stamps the provider on the
manager's gate, and the fallback walk skips the provider for a short window.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict

IDLE, INITIALIZING, READY, FAILED = "idle", "initializing", "ready", "failed"

#: Seconds a provider is skipped by the fallback walk after an auth/5xx error.
CALL_ERROR_WINDOW_SEC = 60.0


@dataclass
class _Entry:
    state: str = IDLE
    attempts: int = 0
    failed_at: float = 0.0


class ProviderInitGate:
    def __init__(self, base: float = 30.0, cap: float = 600.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.base, self.cap, self._clock = base, cap, clock
        self._entries: Dict[str, _Entry] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._call_errors: Dict[str, float] = {}

    def lock(self, provider: str) -> asyncio.Lock:
        if provider not in self._locks:
            self._locks[provider] = asyncio.Lock()
        return self._locks[provider]

    def state(self, provider: str) -> str:
        return self._entries.get(provider, _Entry()).state

    def retry_in(self, provider: str) -> float:
        e = self._entries.get(provider)
        if e is None or e.state != FAILED:
            return 0.0
        delay = min(self.cap, self.base * (2 ** max(0, e.attempts - 1)))
        return max(0.0, e.failed_at + delay - self._clock())

    def may_try(self, provider: str) -> bool:
        return self.retry_in(provider) <= 0.0

    def begin(self, provider: str) -> None:
        self._entries.setdefault(provider, _Entry()).state = INITIALIZING

    def succeeded(self, provider: str) -> None:
        self._entries[provider] = _Entry(state=READY)

    def failed(self, provider: str) -> None:
        e = self._entries.setdefault(provider, _Entry())
        e.state, e.attempts, e.failed_at = FAILED, e.attempts + 1, self._clock()

    def reset(self) -> None:
        self._entries.clear()
        self._call_errors.clear()

    def note_call_error(self, provider: str) -> None:
        self._call_errors[provider] = self._clock()

    def recent_call_error(self, provider: str, window: float = CALL_ERROR_WINDOW_SEC) -> bool:
        at = self._call_errors.get(provider)
        return at is not None and self._clock() - at < window

    async def acquire(self, provider: str, existing: Callable[[], Any],
                      init: Callable[[], Awaitable[Any]]) -> Any:
        """``existing()`` if set; else run ``init()`` once (under the provider
        lock) when the backoff allows. A None result counts as a failure."""
        async with self.lock(provider):
            got = existing()
            if got is not None:  # a concurrent first call won
                return got
            if not self.may_try(provider):
                return None
            self.begin(provider)
            try:
                client = await init()
            except BaseException:
                self.failed(provider)
                raise
            (self.succeeded if client is not None else self.failed)(provider)
            return client


def provider_of_client_name(client_name: str) -> str:
    """The provider behind a client name: ``openai_client`` and ``openai`` name ``openai``."""
    return (client_name or "").replace("_client", "")


def _provider_of_client(client: Any) -> str:
    spec_name = getattr(getattr(client, "_spec", None), "name", None)
    return spec_name or provider_of_client_name(getattr(client, "name", "") or "")


def note_call_error(client: Any, exc: BaseException) -> None:
    """Stamp the provider of ``client`` when a real call failed with an error that says
    the provider is unusable right now (auth, connection/5xx, permanent/billing). A refusal
    or a content filter is about one request, not the provider: it stamps nothing."""
    try:
        from core.exceptions import (
            LLMAuthenticationError, LLMConnectionError, LLMPermanentError, LLMRefusalError,
        )
        gate = getattr(client, "_health_gate", None)
        if gate is not None and not isinstance(exc, LLMRefusalError) and isinstance(
                exc, (LLMAuthenticationError, LLMConnectionError, LLMPermanentError)):
            gate.note_call_error(_provider_of_client(client))
    except Exception:
        pass


def recent_call_error(client: Any, window: float = CALL_ERROR_WINDOW_SEC) -> bool:
    gate = getattr(client, "_health_gate", None)
    return gate is not None and gate.recent_call_error(_provider_of_client(client), window)
