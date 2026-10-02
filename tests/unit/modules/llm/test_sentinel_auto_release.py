"""A top-up must release the credit latch on its own, without maint.

⚠️ 2026-10-02. OpenRouter hit a 402 at 22:22Z and the sentinel latched it. The
owner topped up at ~08:39Z and polyrob was restarted, but the latch file
survives a restart and was re-armed by later 402s. Every cron tick and goal
dispatch kept logging `credit-dead — $0 skip` until a human ran
`release("openrouter")` at 09:14Z. That was 35 minutes of a funded Rob sitting
dead.

`release_funded_latches` closes that gap. For a latched provider that publishes
a FREE balance check (OpenRouter `/credits`), it asks the provider directly and
releases only when the provider itself says the account can pay. The usual
seat-probe rules apply: `unknown`, `unreachable` and `no_credit` never release,
and a provider with no free check (z.ai costs a token to probe) is never probed.
It is rate-limited so a still-dry account costs one free GET per interval,
not one per tick.
"""
import json
import time

import pytest

from core import credit_sentinel as cs
from modules.llm import seat_probe as sp


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _Http:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return self.response

    def post(self, url, **kw):  # pragma: no cover - must never be used here
        self.calls.append(("POST", url))
        return self.response


def _credits(total, used):
    return _Resp(200, {"data": {"total_credits": total, "total_usage": used}})


@pytest.fixture()
def latch(tmp_path, monkeypatch):
    path = tmp_path / "CREDIT_SENTINEL"
    monkeypatch.setattr(cs, "_sentinel_path", lambda: str(path))
    monkeypatch.delenv("CREDIT_SENTINEL_ENABLED", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    sp._LAST_RECOVERY_CHECK.clear()
    return path


def _write(path, providers):
    path.write_text(json.dumps({"providers": providers}))


def _fresh():
    return {"ts": time.time(), "release_ts": None, "reason": "402"}


def test_a_funded_openrouter_is_released(latch):
    _write(latch, {"openrouter": _fresh()})
    http = _Http(_credits(205, 195.16))
    assert sp.release_funded_latches(http=http) == ["openrouter"]
    assert cs.credit_sentinel_active("openrouter") is False
    assert http.calls == [("GET", sp.OPENROUTER_CREDITS_URL)], "the check must stay free"


def test_a_still_dry_openrouter_stays_latched(latch):
    _write(latch, {"openrouter": _fresh()})
    assert sp.release_funded_latches(http=_Http(_credits(195, 195.12))) == []
    assert cs.credit_sentinel_active("openrouter") is True


@pytest.mark.parametrize("resp", [_Resp(500, None), _Resp(200, None), _Resp(401, {})])
def test_an_unmeasured_balance_never_releases(latch, resp):
    _write(latch, {"openrouter": _fresh()})
    assert sp.release_funded_latches(http=_Http(resp)) == []
    assert cs.credit_sentinel_active("openrouter") is True


def test_a_provider_without_a_free_check_is_never_probed(latch):
    _write(latch, {"zai-coding": _fresh()})
    http = _Http(_credits(1000, 0))
    assert sp.release_funded_latches(http=http) == []
    assert http.calls == [], "z.ai costs a token to probe; recovery must not spend it"
    assert cs.credit_sentinel_active("zai-coding") is True


def test_only_the_funded_provider_is_released(latch):
    _write(latch, {"openrouter": _fresh(), "zai-coding": _fresh()})
    assert sp.release_funded_latches(http=_Http(_credits(205, 195))) == ["openrouter"]
    assert cs.credit_sentinel_active("zai-coding") is True


def test_a_global_trip_is_not_released_by_one_providers_balance(latch):
    _write(latch, {cs.GLOBAL_KEY: _fresh()})
    http = _Http(_credits(205, 195))
    assert sp.release_funded_latches(http=http) == []
    assert cs.credit_sentinel_active("openrouter") is True


def test_rate_limited_per_provider(latch):
    _write(latch, {"openrouter": _fresh()})
    http = _Http(_credits(195, 195.12))
    sp.release_funded_latches(http=http, now=1000.0, interval=300)
    sp.release_funded_latches(http=http, now=1100.0, interval=300)
    assert len(http.calls) == 1, "a dry account costs one free GET per interval, not per tick"
    sp.release_funded_latches(http=http, now=1301.0, interval=300)
    assert len(http.calls) == 2


def test_no_latch_means_no_network(latch):
    http = _Http(_credits(205, 0))
    assert sp.release_funded_latches(http=http) == []
    assert http.calls == []


def test_no_key_means_no_release(latch, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _write(latch, {"openrouter": _fresh()})
    assert sp.release_funded_latches(http=_Http(_credits(205, 0))) == []
    assert cs.credit_sentinel_active("openrouter") is True


def test_never_raises(latch, monkeypatch):
    _write(latch, {"openrouter": _fresh()})

    class _Boom:
        def get(self, *a, **k):
            raise RuntimeError("network down")

    assert sp.release_funded_latches(http=_Boom()) == []
    assert cs.credit_sentinel_active("openrouter") is True
