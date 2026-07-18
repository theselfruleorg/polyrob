"""Regression (P0): x402_fetch must run the wallet PolicyGate even when the
price probe can't price the resource.

quote() is advisory — RealX402Client returns None on any error, a non-402 bare
GET, or a method/body-specific paywall. The original code ran wallet.policy.check()
ONLY inside `if price is not None:`, so a None probe skipped the per-tx ceiling /
daily cap / per-venue cap entirely and auto-paid up to the agent-chosen
max_amount_usd (which has no upper bound). This models the gap the shipped
FakeX402Client can't: quote()->None but the real request DOES pay.
"""
import pytest

from core.wallet.config import WalletConfig
from core.wallet.agent_wallet import AgentWallet
from tools.x402.client import X402Result
from tools.x402.service import X402PayTool, FetchParams


def _wallet():
    cfg = WalletConfig(enabled=True, backend="local_eoa", master_seed="s" * 40,
                       network="testnet", max_per_tx_usd=10.0,
                       x402_client_enabled=True, x402_facilitator_url="http://f")
    return AgentWallet(cfg)


class _UnpriceableButPayingClient:
    """Bare-GET probe can't price it (quote->None), but the real request pays."""

    def __init__(self):
        self.fetched = False

    async def quote(self, url):
        return None

    async def fetch_with_payment(self, *, url, method, body, signer, network,
                                 max_amount_usd):
        self.fetched = True
        return X402Result(body="SECRET-DATA", paid=True, amount_usd=max_amount_usd,
                          tx_hash="0xfake", pay_to="0xR", status_code=200)


class _CheapQuoteExpensivePayClient:
    """Advisory quote prices it cheaply, but the real request pays up to the
    caller's max_amount_usd. Models the C1 fund-drain: a malicious paywall that
    quotes $0.01 to the advisory probe then charges the full authorized ceiling."""

    def __init__(self):
        self.fetched = False

    async def quote(self, url):
        return 0.01

    async def fetch_with_payment(self, *, url, method, body, signer, network,
                                 max_amount_usd):
        self.fetched = True
        return X402Result(body="SECRET-DATA", paid=True, amount_usd=max_amount_usd,
                          tx_hash="0xfake", pay_to="0xR", status_code=200)


@pytest.mark.asyncio
async def test_cheap_quote_does_not_authorize_spend_above_ceiling():
    """C1: the PolicyGate must be checked at the AUTHORIZED worst case
    (max_amount_usd), not the advisory quote. A $0.01 quote with a $100
    authorization above the $10 per-tx ceiling must be blocked — the real
    payment can be anything up to max_amount_usd."""
    client = _CheapQuoteExpensivePayClient()
    tool = X402PayTool(wallet=_wallet(), client=client)
    res = await tool.x402_fetch(FetchParams(url="http://paid", max_amount_usd=100.0))
    assert res.error is not None, "gate must authorize at the ceiling, not the cheap quote"
    assert client.fetched is False, "no payment once the gate blocks"


@pytest.mark.asyncio
async def test_unpriceable_probe_still_enforces_catastrophic_ceiling():
    client = _UnpriceableButPayingClient()
    tool = X402PayTool(wallet=_wallet(), client=client)
    # max_amount_usd 100 >> wallet ceiling 10 → PolicyGate must block, no payment.
    res = await tool.x402_fetch(FetchParams(url="http://paid", max_amount_usd=100.0))
    assert res.error is not None, "PolicyGate must block a spend above the per-tx ceiling"
    assert client.fetched is False, "fetch_with_payment must not run once the gate blocks"


@pytest.mark.asyncio
async def test_unpriceable_probe_within_ceiling_still_pays():
    client = _UnpriceableButPayingClient()
    tool = X402PayTool(wallet=_wallet(), client=client)
    res = await tool.x402_fetch(FetchParams(url="http://paid", max_amount_usd=1.0))
    assert res.error is None
    assert client.fetched is True
    assert "SECRET-DATA" in (res.extracted_content or "")


# --- C1 half 2 wiring: the service layer must thread the wallet's PolicyGate --
# --- into the RealX402Client it constructs, so the client can re-check the ----
# --- SDK-selected requirement's REAL amount before signing (see -------------
# --- test_real_client_sdk_integration.py for the end-to-end abort). ----------


# --- M4: check() -> spend -> record() must be atomic per PolicyGate instance -
# --- (commit fe1fd995 added PolicyGate.reserve(); this proves service.py -----
# --- actually wraps the value-moving span in it). -----------------------------

import asyncio


class _SlowPayingClient:
    """quote() can't price it (forces check_amount = max_amount_usd, matching
    the real "unpriceable probe" path), and fetch_with_payment() awaits a short
    sleep before reporting paid=True — long enough that, absent serialization,
    two concurrent x402_fetch calls both pass check() on the SAME stale
    rolling-spend before either has recorded."""

    def __init__(self, delay: float = 0.05):
        self._delay = delay
        self.calls = 0

    async def quote(self, url):
        return None

    async def fetch_with_payment(self, *, url, method, body, signer, network,
                                 max_amount_usd):
        self.calls += 1
        my_call = self.calls
        await asyncio.sleep(self._delay)
        return X402Result(body="DATA", paid=True, amount_usd=max_amount_usd,
                          tx_hash=f"0xrace{my_call}", pay_to="0xR", status_code=200)


@pytest.mark.asyncio
async def test_concurrent_spends_against_nearly_exhausted_cap_cannot_both_pass():
    """M4: two concurrent x402_fetch calls, each individually within the daily
    cap when checked against a STALE (pre-either-spend) rolling total, but
    together over it. Exactly one must succeed and one must be refused — never
    both. (daily_cap=1.0, two spends of 0.6 each: 0.6<=1.0 alone, 1.2>1.0
    together.)"""
    w = _wallet()
    w._policy._daily_cap = 1.0  # nearly-exhausted cap for this test's amounts
    client = _SlowPayingClient(delay=0.05)
    # Distinct urls -> distinct idempotency keys, so the replay-guard is not
    # what blocks the second call; only the daily-cap race matters here.
    tool_a = X402PayTool(wallet=w, client=client)
    tool_b = X402PayTool(wallet=w, client=client)

    res_a, res_b = await asyncio.gather(
        tool_a.x402_fetch(FetchParams(url="http://paid/a", max_amount_usd=0.6)),
        tool_b.x402_fetch(FetchParams(url="http://paid/b", max_amount_usd=0.6)),
    )

    outcomes = [res_a, res_b]
    succeeded = [r for r in outcomes if r.error is None]
    refused = [r for r in outcomes if r.error is not None]
    assert len(succeeded) == 1, (
        f"expected exactly ONE spend to pass a nearly-exhausted cap, got "
        f"{len(succeeded)} (both results: {outcomes})"
    )
    assert len(refused) == 1
    assert "cap" in refused[0].error.lower()
    # Only the winning spend actually reached the client's paying leg.
    assert client.calls == 1, (
        f"the losing call must be refused BEFORE the network pay leg runs, "
        f"got {client.calls} paying calls"
    )
    total_recorded = sum(e["amount_usd"] for e in w.policy.audit_log if e["venue"] == "x402")
    assert total_recorded <= 1.0, f"cap bypassed: recorded ${total_recorded:.2f} against a $1.00 cap"


def test_service_threads_wallet_policy_into_real_client(monkeypatch):
    """service._get_client must construct RealX402Client(policy=<wallet gate>)
    so the client's before-signing re-check layer is armed. Without the policy
    threaded in, the real amount the SDK selects is never re-checked against the
    catastrophic ceiling / daily cap inside _abort_if_invalid_requirement."""
    import tools.x402.real_client as rc

    captured = {}

    class _StubRealClient:
        def __init__(self, policy=None):
            captured["policy"] = policy

    monkeypatch.setattr(rc, "RealX402Client", _StubRealClient)
    w = _wallet()
    tool = X402PayTool(wallet=w)  # no client injected -> _get_client constructs
    tool._get_client()
    assert captured["policy"] is w.policy
