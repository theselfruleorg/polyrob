"""Shared isolation for the api unit tests."""
import pytest


@pytest.fixture(autouse=True)
def _reset_treasury_cache():
    """B41 caches the advertised treasury address per PROCESS.

    Several tests here monkeypatch the wallet or the ``X402_PAYMENT_*`` env the
    resolver reads; without this reset the first test's answer would be served
    to every later one (and to any test that runs after them in a full suite).
    """
    from api.x402_advertisement import reset_treasury_cache
    reset_treasury_cache()
    yield
    reset_treasury_cache()
