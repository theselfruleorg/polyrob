"""C8: live ETH/USD price oracle for the deposit monitor. Fails CLOSED
(raises) on a bad fetch — see module docstring for why a silent stale
fallback is unacceptable here (it would misprice a real deposit).
"""
import pytest

from modules.payments.price_oracle import get_eth_price_usd


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeHttpClient:
    def __init__(self, payload):
        self._payload = payload

    async def get(self, url, params=None):
        return _FakeResponse(self._payload)


class _FailingHttpClient:
    async def get(self, url, params=None):
        return _FakeResponse({}, status=503)


@pytest.mark.asyncio
async def test_fetches_live_price(monkeypatch):
    monkeypatch.delenv("ETH_PRICE_USD_OVERRIDE", raising=False)
    client = _FakeHttpClient({"ethereum": {"usd": 3456.78}})
    price = await get_eth_price_usd(http_client=client)
    assert price == 3456.78


@pytest.mark.asyncio
async def test_env_override_short_circuits_the_fetch(monkeypatch):
    monkeypatch.setenv("ETH_PRICE_USD_OVERRIDE", "1234.5")
    # No client passed at all -> would blow up if the override didn't short-circuit.
    price = await get_eth_price_usd()
    assert price == 1234.5


@pytest.mark.asyncio
async def test_fails_closed_on_bad_response(monkeypatch):
    monkeypatch.delenv("ETH_PRICE_USD_OVERRIDE", raising=False)
    client = _FailingHttpClient()
    with pytest.raises(Exception):
        await get_eth_price_usd(http_client=client)
