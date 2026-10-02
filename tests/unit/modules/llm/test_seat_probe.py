"""Can this seat actually serve RIGHT NOW? Measured, not inferred.

⚠️ 2026-09-24, the mistake this exists to prevent. `polyrob doctor` reported
that the credit sentinel "does NOT block live provider zai-coding", which was
true — the latch is per provider and zai was never latched. I read that as "zai
is a working escape hatch" and offered it to the owner in the daily digest
without making one call against it. It was out of funds
(`429 · 1113 · Insufficient balance`), and I had to correct the message 18
minutes later.

"Not blocked" is a statement about OUR latch. "Usable" is a statement about the
PROVIDER'S account, and only the provider can make it. The gap between them is
this module.

Two rules the design turns on:

* **Prefer a free check.** OpenRouter publishes a credits endpoint, so asking it
  costs nothing; a completion would cost money to learn the same thing. A probe
  you are reluctant to run is a probe nobody runs.
* **No probe is not a pass.** A provider we have no way to check reports
  `unknown`, never `ok` — the whole failure being fixed here is a confident
  claim with no measurement under it.
"""
import json

import pytest

from modules.llm import seat_probe as sp


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Http:
    """Records calls so a test can assert a probe stayed free."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return self.response

    def post(self, url, **kw):
        self.calls.append(("POST", url))
        return self.response


# --- openrouter: the free credits read ------------------------------------- #

def test_openrouter_with_money_is_ok(monkeypatch):
    http = _Http(_Resp(200, {"data": {"total_credits": 185.0,
                                      "total_usage": 100.0}}))
    v = sp.probe("openrouter", api_key="k", http=http)
    assert v.state == "ok"
    assert v.remaining_usd == pytest.approx(85.0)


def test_openrouter_overdrawn_is_no_credit(monkeypatch):
    """The live shape on 2026-09-24: 185 issued, 185.14 used."""
    http = _Http(_Resp(200, {"data": {"total_credits": 185.0,
                                      "total_usage": 185.140035014}}))
    v = sp.probe("openrouter", api_key="k", http=http)
    assert v.state == "no_credit"
    assert v.remaining_usd == pytest.approx(-0.140035014)
    assert "-0.14" in v.detail


def test_exactly_zero_is_no_credit(monkeypatch):
    """Zero buys nothing. The boundary belongs on the refusing side."""
    http = _Http(_Resp(200, {"data": {"total_credits": 5.0, "total_usage": 5.0}}))
    assert sp.probe("openrouter", api_key="k", http=http).state == "no_credit"


def test_the_openrouter_probe_costs_nothing(monkeypatch):
    http = _Http(_Resp(200, {"data": {"total_credits": 1.0, "total_usage": 0.0}}))
    v = sp.probe("openrouter", api_key="k", http=http)
    assert [m for m, _ in http.calls] == ["GET"], "a completion would cost money"
    assert v.free_check is True


def test_a_bad_openrouter_key_is_auth_not_no_credit(monkeypatch):
    """Distinguished on purpose: a top-up fixes one and not the other."""
    http = _Http(_Resp(401, None, "unauthorized"))
    assert sp.probe("openrouter", api_key="k", http=http).state == "auth"


# --- zai-coding: a minimal completion, because there is no free read -------- #

def test_zai_insufficient_balance_is_no_credit():
    """z.ai dresses credit death as a 429. The code, not the status, is the
    signal — treating this as a rate limit would make it look retryable."""
    http = _Http(_Resp(429, {"error": {"code": "1113", "message":
                                       "[1113][Insufficient balance or no "
                                       "resource package. Please recharge.]"}}))
    v = sp.probe("zai-coding", api_key="k", http=http)
    assert v.state == "no_credit"
    assert "1113" in v.detail


def test_zai_serving_is_ok():
    http = _Http(_Resp(200, {"content": [{"type": "text", "text": "hi"}]}))
    v = sp.probe("zai-coding", api_key="k", http=http)
    assert v.state == "ok"
    assert v.free_check is False, "this one costs a token or two — say so"


def test_a_real_rate_limit_is_not_credit_death():
    """A plain 429 with no credit code is turbulence, not an empty account."""
    http = _Http(_Resp(429, {"error": {"code": "1302", "message":
                                       "too many concurrent requests"}}))
    assert sp.probe("zai-coding", api_key="k", http=http).state == "rate_limited"


def test_a_zai_plan_exhaustion_is_no_credit():
    http = _Http(_Resp(429, {"error": {"code": "1310", "message":
                                       "Weekly Limit Exhausted. Your limit "
                                       "will reset at 2026-09-25T00:00Z"}}))
    assert sp.probe("zai-coding", api_key="k", http=http).state == "no_credit"


# --- the honest non-answers ------------------------------------------------ #

def test_a_provider_with_no_probe_is_unknown_never_ok():
    v = sp.probe("some-new-provider", api_key="k", http=_Http(_Resp(200, {})))
    assert v.state == "unknown"
    assert "no probe" in v.detail.lower()


def test_a_missing_key_is_unknown_not_a_failure_of_the_account():
    v = sp.probe("openrouter", api_key="", http=_Http(_Resp(200, {})))
    assert v.state == "unknown"
    assert "key" in v.detail.lower()


def test_a_network_failure_is_unreachable_not_no_credit():
    class _Boom:
        def get(self, *a, **kw):
            raise OSError("connection reset")

        post = get

    v = sp.probe("openrouter", api_key="k", http=_Boom())
    assert v.state == "unreachable"
    assert "connection reset" in v.detail


def test_an_unparseable_body_is_unknown():
    v = sp.probe("openrouter", api_key="k", http=_Http(_Resp(200, None, "<html>")))
    assert v.state == "unknown"


# --- the verdict carries its own provenance -------------------------------- #

def test_every_verdict_says_when_it_was_measured():
    http = _Http(_Resp(200, {"data": {"total_credits": 1.0, "total_usage": 0.0}}))
    v = sp.probe("openrouter", api_key="k", http=http)
    assert v.measured_at > 0
    assert v.provider == "openrouter"


def test_usable_is_true_only_for_ok():
    def _v(state):
        return sp.SeatVerdict(provider="p", state=state, detail="",
                              measured_at=1.0, free_check=True)

    assert _v("ok").usable is True
    for state in ("no_credit", "auth", "unreachable", "unknown", "rate_limited"):
        assert _v(state).usable is False, f"{state} must not read as usable"
