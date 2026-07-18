import pytest
from core.wallet.config import WalletConfig
from core.wallet.agent_wallet import AgentWallet
from tools.x402.client import FakeX402Client, X402Result
from tools.x402.service import X402PayTool, FetchParams, QuoteParams, EmptyWalletParams


class _ResultClient:
    """Test double that returns an exact, caller-specified X402Result — lets a
    test drive amount_is_estimate / paid without FakeX402Client's fixed shape.
    Mirrors the _SpyClient pattern below (G-11)."""

    def __init__(self, result: X402Result, price_usd: float = 0.1):
        self._result = result
        self._price = price_usd
        self.fetch_called = False

    async def quote(self, url):
        return self._price

    async def fetch_with_payment(self, **kwargs):
        self.fetch_called = True
        return self._result


def _wallet():
    cfg = WalletConfig(enabled=True, backend="local_eoa", master_seed="s" * 40,
                       network="testnet", max_per_tx_usd=10.0,
                       x402_client_enabled=True, x402_facilitator_url="http://f")
    return AgentWallet(cfg)


def _tool(client):
    return X402PayTool(wallet=_wallet(), client=client)


@pytest.mark.asyncio
async def test_quote_reports_price():
    tool = _tool(FakeX402Client(price_usd=0.25, pay_to="0xR", paid_body="X"))
    res = await tool.x402_quote(QuoteParams(url="http://paid"))
    assert "0.25" in res.extracted_content


@pytest.mark.asyncio
async def test_fetch_pays_and_returns_body():
    tool = _tool(FakeX402Client(price_usd=0.25, pay_to="0xR", paid_body="SECRET-DATA"))
    res = await tool.x402_fetch(FetchParams(url="http://paid", max_amount_usd=1.0))
    assert "SECRET-DATA" in res.extracted_content
    assert res.error is None


@pytest.mark.asyncio
async def test_fetch_rejects_over_cap():
    tool = _tool(FakeX402Client(price_usd=5.0, pay_to="0xR", paid_body="X"))
    res = await tool.x402_fetch(FetchParams(url="http://paid", max_amount_usd=1.0))
    assert res.error is not None and "exceeds" in res.error.lower()


@pytest.mark.asyncio
async def test_fetch_rejects_over_catastrophic_ceiling():
    # wallet ceiling is 10.0; ask price 50 but cap high → PolicyGate blocks
    tool = _tool(FakeX402Client(price_usd=50.0, pay_to="0xR", paid_body="X"))
    res = await tool.x402_fetch(FetchParams(url="http://paid", max_amount_usd=100.0))
    assert res.error is not None and "ceiling" in res.error.lower()


@pytest.mark.asyncio
async def test_result_never_contains_private_key():
    w = _wallet()
    raw = w._derive_key("x402").hex()
    tool = X402PayTool(wallet=w, client=FakeX402Client(price_usd=0.1, pay_to="0xR", paid_body="X"))
    res = await tool.x402_fetch(FetchParams(url="http://paid", max_amount_usd=1.0))
    assert raw not in (res.extracted_content or "").lower()


@pytest.mark.asyncio
async def test_wallet_status_reports_addresses_not_keys():
    w = _wallet()
    raw = w._derive_key("x402").hex()
    tool = X402PayTool(wallet=w, client=FakeX402Client(price_usd=None, pay_to=None, paid_body="X"))
    from tools.x402.service import EmptyWalletParams
    res = await tool.x402_wallet_status(EmptyWalletParams())
    # x402 now pays from the OPERATIONAL venue (default treasury) — the surfaced
    # address must be that spend address, and never a private key.
    assert w.operational_signer().address in res.extracted_content
    assert raw not in res.extracted_content.lower()
    assert w._derive_key("treasury").hex() not in res.extracted_content.lower()


@pytest.mark.asyncio
async def test_disabled_wallet_errors_cleanly():
    tool = X402PayTool(wallet=None, client=FakeX402Client(price_usd=0.1, pay_to="0xR", paid_body="X"))
    res = await tool.x402_fetch(FetchParams(url="http://paid", max_amount_usd=1.0))
    assert res.error is not None and "not enabled" in res.error.lower()


# --- G-10: idempotency key stable across quote-availability changes ---------

@pytest.mark.asyncio
async def test_idem_key_stable_across_quote_availability_changes():
    """The key must be derived from the AUTHORIZED ceiling (max_amount_usd), not
    from check_amount (quote-price-or-ceiling). Two attempts at the same url with
    the same max_amount_usd — one where quote() is unavailable (None -> falls
    back to the ceiling) and one where it prices the resource — must produce the
    IDENTICAL idempotency key, or a retried step breaks the replay-guard's
    ability to correlate it with the prior attempt (double-pay risk)."""
    w = _wallet()
    seen_keys = []
    orig_check = w.policy.check

    def spy_check(*, venue, amount_usd, idempotency_key):
        seen_keys.append(idempotency_key)
        return orig_check(venue=venue, amount_usd=amount_usd, idempotency_key=idempotency_key)

    w.policy.check = spy_check

    # Attempt 1: quote() unavailable -> check_amount falls back to the ceiling.
    # FakeX402Client(price_usd=None) makes fetch_with_payment return paid=False,
    # so record() never fires and the key is NOT marked "seen" (check() must not
    # act as a reservation — a checked-but-failed fetch must stay retryable).
    tool1 = X402PayTool(wallet=w, client=FakeX402Client(price_usd=None, pay_to="0xR", paid_body="X"))
    res1 = await tool1.x402_fetch(FetchParams(url="http://paid", max_amount_usd=1.0))
    assert res1.error is None

    # Attempt 2 (retry of the SAME logical fetch): quote() now prices it.
    tool2 = X402PayTool(wallet=w, client=FakeX402Client(price_usd=0.2, pay_to="0xR", paid_body="X"))
    res2 = await tool2.x402_fetch(FetchParams(url="http://paid", max_amount_usd=1.0))
    assert res2.error is None

    assert len(seen_keys) == 2
    assert seen_keys[0] == seen_keys[1] == "x402:http://paid:1.0"


# --- G-11: kill-switch probe must fail CLOSED, not swallow the exception ----

class _SpyClient:
    """Records whether it was ever reached — used to prove a refused payment
    never touches the payment client at all."""

    def __init__(self):
        self.quote_called = False
        self.fetch_called = False

    async def quote(self, url):
        self.quote_called = True
        return 0.1

    async def fetch_with_payment(self, **kwargs):
        self.fetch_called = True
        return X402Result(body="X", paid=True, amount_usd=0.1, tx_hash="0x1",
                          pay_to="0xR", status_code=200)


@pytest.mark.asyncio
async def test_kill_switch_probe_failure_fails_closed(monkeypatch):
    """G-11: service.py used to wrap the autonomy_halted() probe in a bare
    `except Exception: pass`, so an import/probe failure silently disabled the
    halt check on a money path. Simulate that failure and assert the payment is
    now REFUSED (not silently allowed through) and the client is never reached."""
    from agents.task.constants import AutonomyConfig

    def _raise():
        raise RuntimeError("boom: simulated import/probe failure")

    monkeypatch.setattr(AutonomyConfig, "autonomy_halted", staticmethod(_raise))

    spy = _SpyClient()
    tool = X402PayTool(wallet=_wallet(), client=spy)
    res = await tool.x402_fetch(FetchParams(url="http://paid", max_amount_usd=1.0))

    assert res.error is not None
    assert "kill-switch" in res.error.lower() or "boom" in res.error
    assert spy.quote_called is False
    assert spy.fetch_called is False


# --- G-12: wallet-status summary must count/sum ONLY the x402 venue --------

@pytest.mark.asyncio
async def test_wallet_status_summarizes_only_x402_venue():
    """G-12: wallet.policy.audit_log spans ALL venues (and, with the persistent
    JSONL sink, the wallet's whole lifetime — not "this process"). The status
    line must count/sum the SAME filtered (venue == "x402") set it claims to
    describe, and must not mislabel a lifetime/all-venue count as scoped."""
    w = _wallet()
    w.policy.record(venue="x402", action="pay", amount_usd=0.10, counterparty="0xA",
                    idempotency_key="k1", result_ref="tx1")
    w.policy.record(venue="x402", action="pay", amount_usd=0.20, counterparty="0xB",
                    idempotency_key="k2", result_ref="tx2")
    w.policy.record(venue="hyperliquid", action="trade", amount_usd=50.0, counterparty="0xC",
                    idempotency_key="k3", result_ref="tx3")

    tool = X402PayTool(wallet=w, client=FakeX402Client(price_usd=None, pay_to=None, paid_body="X"))
    res = await tool.x402_wallet_status(EmptyWalletParams())

    assert res.error is None
    assert "x402 payments (all recorded): 2 (≈$0.3000)" in res.extracted_content
    # The hyperliquid entry's $50 must never leak into the x402-labeled sum/count.
    assert "50.0000" not in res.extracted_content
    assert ": 3 " not in res.extracted_content


# --- Task 4 review Finding 1 (G-6 scope): a client that reports paid=False --
# --- (e.g. because settlement failed) must never be recorded as a payment. -

@pytest.mark.asyncio
async def test_client_paid_false_is_never_recorded_as_a_payment():
    """Service-layer seam for Finding 1: whatever the reason a client reports
    paid=False (real_client.py now does this for a settle success=False —
    see test_reconcile_settle_success_false_flips_to_unpaid_and_logs_loudly in
    test_real_client_smoke.py), the service must not call wallet.policy.record
    and must not surface a '[paid ...]' audit header."""
    w = _wallet()
    recorded = []
    orig_record = w.policy.record

    def spy_record(**kwargs):
        recorded.append(kwargs)
        return orig_record(**kwargs)

    w.policy.record = spy_record

    result = X402Result(body="SECRET-DATA", paid=False, amount_usd=0.0, tx_hash="0xDEADBEEF",
                        pay_to="0xR", status_code=200, amount_is_estimate=False)
    tool = X402PayTool(wallet=w, client=_ResultClient(result))
    res = await tool.x402_fetch(FetchParams(url="http://paid", max_amount_usd=1.0))

    assert res.error is None
    assert recorded == []
    assert "[paid" not in (res.extracted_content or "")
    assert "SECRET-DATA" in res.extracted_content


# --- Finding 2 (cheap, related): amount_is_estimate must be surfaced -------

@pytest.mark.asyncio
async def test_header_marks_estimated_amount():
    """When the client reports amount_is_estimate=True, the user/audit-facing
    header must say so — distinguishing a pre-settlement estimate from a
    confirmed-settled figure."""
    w = _wallet()
    result = X402Result(body="SECRET-DATA", paid=True, amount_usd=0.05, tx_hash="0xTXHASH",
                        pay_to="0xR", status_code=200, amount_is_estimate=True)
    tool = X402PayTool(wallet=w, client=_ResultClient(result))
    res = await tool.x402_fetch(FetchParams(url="http://paid", max_amount_usd=1.0))

    assert res.error is None
    assert "(estimated)" in res.extracted_content
    assert "[paid $0.0500 (estimated) to 0xR, tx 0xTXHASH]" in res.extracted_content


@pytest.mark.asyncio
async def test_header_omits_estimated_marker_when_confirmed():
    """When the client reports a confirmed (non-estimate) settled amount, the
    header must stay clean — no '(estimated)' marker."""
    w = _wallet()
    result = X402Result(body="SECRET-DATA", paid=True, amount_usd=0.06, tx_hash="0xTXHASH",
                        pay_to="0xR", status_code=200, amount_is_estimate=False)
    tool = X402PayTool(wallet=w, client=_ResultClient(result))
    res = await tool.x402_fetch(FetchParams(url="http://paid", max_amount_usd=1.0))

    assert res.error is None
    assert "(estimated)" not in res.extracted_content
    assert "[paid $0.0600 to 0xR, tx 0xTXHASH]" in res.extracted_content
