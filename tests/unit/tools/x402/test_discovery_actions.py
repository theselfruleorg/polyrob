"""The agent-callable discovery verbs: x402_probe / x402_sweep, and a
wallet-free x402_quote.

Pricing an endpoint costs $0, so it must not require a funded wallet — the old
``x402_quote`` refused outright when AGENT_WALLET_ENABLED was off, which left an
invoice-only deployment unable to see what anything charges.
"""
import json

import pytest

from tools.x402.service import (
    X402PayTool, QuoteParams, ProbeParams, SweepParams,
)


class _FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self.headers = {}
        self.text = json.dumps(body) if body is not None else ""


CHALLENGE = {
    "x402Version": 1,
    "accepts": [{"scheme": "exact", "network": "base", "maxAmountRequired": "10000",
                 "asset": "0xUSDC", "payTo": "0xTreasury"}],
}


class _AllowAll:
    def validate_and_resolve(self, url):
        return True, None, None


def _fetch_402(*_a, **_k):
    async def _f(url, **kwargs):
        return _FakeResponse(402, CHALLENGE)
    return _f


@pytest.mark.asyncio
async def test_probe_works_with_no_wallet_at_all():
    """Discovery is $0 — a wallet-less deploy must still see the paywall."""
    tool = X402PayTool(wallet=None, client=None)
    tool._wallet_resolved = True  # no wallet configured on this deployment

    res = await tool.x402_probe(
        ProbeParams(url="https://pay.test/x"),
        _fetch=_fetch_402(), _validator=_AllowAll())

    assert res.error is None
    assert "402" in res.extracted_content
    assert "0.01" in res.extracted_content
    assert "5/5" in res.extracted_content


@pytest.mark.asyncio
async def test_quote_no_longer_requires_a_wallet():
    class _Client:
        async def quote(self, url, **kwargs):
            return 0.25

    tool = X402PayTool(wallet=None, client=_Client())
    tool._wallet_resolved = True

    res = await tool.x402_quote(QuoteParams(url="https://pay.test/x"))

    assert res.error is None
    assert "0.25" in res.extracted_content


@pytest.mark.asyncio
async def test_quote_points_at_probe_when_it_finds_no_challenge():
    """GET-only quote misses POST-only paywalls; say so instead of 'it's free'."""
    class _Client:
        async def quote(self, url, **kwargs):
            return None

    tool = X402PayTool(wallet=None, client=_Client())
    tool._wallet_resolved = True

    res = await tool.x402_quote(QuoteParams(url="https://rpc.test/"))

    assert "x402_probe" in res.extracted_content


@pytest.mark.asyncio
async def test_probe_reports_a_dead_endpoint_without_raising():
    tool = X402PayTool(wallet=None, client=None)
    tool._wallet_resolved = True

    async def _boom(url, **kwargs):
        raise ConnectionError("refused")

    res = await tool.x402_probe(
        ProbeParams(url="https://dead.test/x"), _fetch=_boom, _validator=_AllowAll())

    assert res.error is None, "a dead endpoint is data, not a tool failure"
    assert "ConnectionError" in res.extracted_content
    assert "0/5" in res.extracted_content


@pytest.mark.asyncio
async def test_sweep_returns_a_scored_ledger():
    tool = X402PayTool(wallet=None, client=None)
    tool._wallet_resolved = True

    res = await tool.x402_sweep(
        SweepParams(targets=["https://a.test/p", "https://b.test/p"]),
        _fetch=_fetch_402(), _validator=_AllowAll())

    assert res.error is None
    assert "2" in res.extracted_content
    assert "payable" in res.extracted_content.lower()


@pytest.mark.asyncio
async def test_sweep_refuses_an_oversized_target_list():
    from tools.x402.discovery import MAX_SWEEP_TARGETS

    tool = X402PayTool(wallet=None, client=None)
    tool._wallet_resolved = True

    res = await tool.x402_sweep(
        SweepParams(targets=[f"https://t{i}.test/" for i in range(MAX_SWEEP_TARGETS + 1)]),
        _fetch=_fetch_402(), _validator=_AllowAll())

    assert res.error is not None
    assert "at most" in res.error


def test_probe_and_sweep_are_declared_actions():
    """They must be reachable BY THE AGENT, not merely importable by us."""
    assert X402PayTool.x402_probe._param_model is ProbeParams
    assert X402PayTool.x402_sweep._param_model is SweepParams
    for verb in (X402PayTool.x402_probe, X402PayTool.x402_sweep):
        assert verb._description, "an action needs a description the model can read"
        assert "never pays" in verb._description.lower(), \
            "the $0 contract must be stated where the model actually reads it"
