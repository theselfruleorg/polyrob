"""Ask a provider whether it can serve RIGHT NOW, instead of inferring it.

⚠️ **2026-09-24.** `polyrob doctor` correctly reported that the credit sentinel
does not block `zai-coding` — the latch is per provider and zai was never
latched. That was read as "zai is a working escape hatch" and offered to the
owner as one of two ways out of a credit wall, without a single call against it.
z.ai was out of funds (`429 · 1113 · Insufficient balance`), and the advice had
to be corrected 18 minutes later.

**"Not blocked" is a statement about OUR latch. "Usable" is a statement about the
PROVIDER'S account, and only the provider can make it.** This module is the gap
between them, and it exists so that claim always has a measurement under it.

Two rules it turns on:

* **Prefer a free check.** OpenRouter publishes a credits endpoint, so asking
  costs nothing; a completion would spend money to learn the same thing. A probe
  people are reluctant to run is a probe nobody runs.
* **No probe is not a pass.** A provider with no defined check reports
  ``unknown`` — never ``ok``. The failure being fixed here IS a confident claim
  with nothing under it, and a default of "fine" would rebuild it.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Optional

#: The provider codes that mean "this account cannot pay", as opposed to
#: "slow down". z.ai dresses BOTH as HTTP 429, so the status alone would make
#: credit death look retryable and a retry storm look like a billing problem.
_ZAI_CREDIT_CODES = ("1113", "1310", "1311")
_CREDIT_TEXT_MARKERS = ("insufficient balance", "insufficient_quota",
                        "limit exhausted", "please recharge", "add credits")

OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
ZAI_MESSAGES_URL = "https://api.z.ai/api/anthropic/v1/messages"

#: Small enough to be a rounding error on any plan, large enough to be a real
#: completion request that the account has to be able to pay for.
_PROBE_MAX_TOKENS = 1


@dataclass
class SeatVerdict:
    provider: str
    state: str          # ok | no_credit | auth | rate_limited | unreachable | unknown
    detail: str
    measured_at: float
    free_check: bool
    remaining_usd: Optional[float] = None

    @property
    def usable(self) -> bool:
        """ONLY ``ok``. Every other state — including ``unknown`` — is "do not
        tell anyone this seat works"."""
        return self.state == "ok"


def _verdict(provider, state, detail, free_check, remaining_usd=None) -> SeatVerdict:
    return SeatVerdict(provider=provider, state=state, detail=detail,
                       measured_at=time.time(), free_check=free_check,
                       remaining_usd=remaining_usd)


class _OwnClient:
    """A bounded, short-lived client, built only when the caller supplied none.

    ⚠️ Deliberately NOT the `tools.defi.providers._http` pool, for two reasons.
    The layering runs `core <- modules <- agents <- tools`, so reaching up into
    `tools` from here is the upward edge the ratchet forbids. And adding a
    SECOND module-level pool to dodge that would be the thing this repo keeps
    saying not to do.

    The cost is honest and small: on this box a new connection burns ~6 s to the
    broken-IPv6 timeout, and a probe runs about once per maintenance tick
    against one or two seats. If probing ever becomes hot, the fix is to move
    the existing pool DOWN into `core` and have both callers share it — not to
    grow a pool here.
    """

    def __init__(self):
        import httpx
        self._c = httpx.Client(timeout=httpx.Timeout(20.0, connect=8.0),
                               follow_redirects=True)

    def __enter__(self):
        return self._c

    def __exit__(self, *exc):
        try:
            self._c.close()
        except Exception:
            pass
        return False


def _looks_like_credit_death(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _CREDIT_TEXT_MARKERS)


def _probe_openrouter(api_key: str, http: Any) -> SeatVerdict:
    resp = http.get(OPENROUTER_CREDITS_URL, timeout=20,
                    headers={"authorization": f"Bearer {api_key}"})
    code = getattr(resp, "status_code", 0)
    if code in (401, 403):
        return _verdict("openrouter", "auth", f"HTTP {code} — the key was refused", True)
    if code >= 400:
        return _verdict("openrouter", "unknown",
                        f"HTTP {code}: {str(getattr(resp, 'text', ''))[:200]}", True)
    try:
        data = (resp.json() or {}).get("data") or {}
        remaining = float(data["total_credits"]) - float(data["total_usage"])
    except Exception as exc:
        return _verdict("openrouter", "unknown",
                        f"credits body did not parse ({type(exc).__name__})", True)
    # Zero buys nothing, so the boundary belongs on the refusing side.
    state = "ok" if remaining > 0 else "no_credit"
    return _verdict("openrouter", state, f"remaining ${remaining:.2f}", True,
                    remaining_usd=remaining)


def _probe_zai(api_key: str, http: Any) -> SeatVerdict:
    """No free balance endpoint, so this one costs a token. Kept minimal."""
    resp = http.post(ZAI_MESSAGES_URL, timeout=25,
                     headers={"authorization": f"Bearer {api_key}",
                              "anthropic-version": "2023-06-01",
                              "content-type": "application/json"},
                     json={"model": "glm-5.3-flash",
                           "max_tokens": _PROBE_MAX_TOKENS,
                           "messages": [{"role": "user", "content": "hi"}]})
    code = getattr(resp, "status_code", 0)
    body = str(getattr(resp, "text", ""))
    if code in (401, 403):
        return _verdict("zai-coding", "auth", f"HTTP {code} — the key was refused", False)
    if code == 429:
        try:
            err = (resp.json() or {}).get("error") or {}
        except Exception:
            err = {}
        err_code = str(err.get("code") or "")
        if err_code in _ZAI_CREDIT_CODES or _looks_like_credit_death(str(err.get("message") or body)):
            return _verdict("zai-coding", "no_credit",
                            f"429 · code {err_code or '?'} · {str(err.get('message') or '')[:160]}",
                            False)
        return _verdict("zai-coding", "rate_limited",
                        f"429 · code {err_code or '?'} — turbulence, not an empty account",
                        False)
    if code >= 400:
        return _verdict("zai-coding", "unknown", f"HTTP {code}: {body[:200]}", False)
    return _verdict("zai-coding", "ok", "a minimal completion was served", False)


_PROBES = {
    "openrouter": _probe_openrouter,
    "zai-coding": _probe_zai,
}


def probe(provider: str, *, api_key: Optional[str], http: Any = None) -> SeatVerdict:
    """Measure whether *provider* can serve, without guessing on its behalf."""
    name = str(provider or "")
    fn = _PROBES.get(name)
    if fn is None:
        return _verdict(name, "unknown",
                        f"no probe is defined for {name!r} — unmeasured, not healthy",
                        True)
    if not api_key:
        return _verdict(name, "unknown", "no API key for this seat in the environment",
                        True)
    try:
        if http is not None:
            return fn(api_key, http)
        with _OwnClient() as client:
            return fn(api_key, client)
    except Exception as exc:
        # A seat we could not reach is not a seat we know to be broke.
        return _verdict(name, "unreachable", f"{type(exc).__name__}: {exc}",
                        True)


# Seats whose balance can be read for free. Recovery never spends a token to
# learn whether a latched seat was topped up (z.ai would cost a completion).
_FREE_RECOVERY_KEYS = {"openrouter": "OPENROUTER_API_KEY"}
RECOVERY_INTERVAL_SEC = 300
_LAST_RECOVERY_CHECK: dict = {}


def release_funded_latches(*, http: Any = None, now: Optional[float] = None,
                           interval: float = RECOVERY_INTERVAL_SEC) -> list:
    """Release a provider's credit latch once the provider itself says it can pay.

    ⚠️ 2026-10-02: a top-up plus a restart left cron and goals skipping for
    35 min because the durable CREDIT_SENTINEL outlives the process. Only a
    free balance check is used, only an ``ok`` verdict releases (``unknown`` /
    ``unreachable`` / ``no_credit`` never do), the global ``*`` trip is left to
    its owner, and each provider is checked at most once per *interval*.
    Never raises; returns the providers it released.
    """
    released: list = []
    try:
        from core.credit_sentinel import credit_sentinel_status, release
        latched = credit_sentinel_status() or {}
        t = time.time() if now is None else now
        for name in latched:
            env_key = _FREE_RECOVERY_KEYS.get(name)
            if env_key is None:
                continue
            last = _LAST_RECOVERY_CHECK.get(name)
            if last is not None and t - last < interval:
                continue
            _LAST_RECOVERY_CHECK[name] = t
            verdict = probe(name, api_key=os.getenv(env_key), http=http)
            if verdict.usable and release(name):
                released.append(name)
    except Exception:
        pass
    return released
