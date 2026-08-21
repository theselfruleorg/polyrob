"""Read-only x402 discovery: probe one endpoint, sweep many, score payability.

An agent that holds a wallet and an invoice rail must be able to FIND
counterparties and judge whether they are actually payable before it spends
anything. That capability was missing from core, so the prod instance hand-built
it as throwaway scripts in its project directory (and lost them to the workspace
GC). These tests pin the behaviour core now owns.

Hard rule under test: discovery NEVER pays and NEVER needs a wallet.
"""
import json

import pytest


# --------------------------------------------------------------------------
# fakes — no test touches the network
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = body if isinstance(body, str) else (
            json.dumps(body) if body is not None else "")


def fake_fetch(mapping):
    """Build a fetch seam: {url: FakeResponse | Exception} -> async callable."""
    async def _fetch(url, *, method="GET", body=None, timeout=None):
        outcome = mapping[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    return _fetch


CHALLENGE = {
    "x402Version": 1,
    "accepts": [
        {"scheme": "exact", "network": "base", "maxAmountRequired": "10000",
         "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "payTo": "0xTreasury"},
        {"scheme": "exact", "network": "base-sepolia", "maxAmountRequired": "10000",
         "asset": "0xSepoliaUSDC", "payTo": "0xTreasury"},
    ],
}

# A 402 that discloses nothing useful — payable in name only.
BARE_402 = {"x402Version": 1, "accepts": [{"scheme": "exact"}]}


def _no_validate():
    """Validator stub that allows every URL (network guard tested separately)."""
    class _V:
        def validate_and_resolve(self, url):
            return True, None, None
    return _V()


# --------------------------------------------------------------------------
# scoring rubric
# --------------------------------------------------------------------------

def test_score_full_marks_for_a_fully_disclosed_402():
    from tools.x402.discovery import score_endpoint

    row = {"status": 402, "error": None, "challenge_parseable": True,
           "price_usd": 0.01, "asset": "0xUSDC", "network": "base", "pay_to": "0xT"}
    score, reasons = score_endpoint(row)

    assert score == 5
    assert reasons == []


def test_score_dead_endpoint_is_zero():
    from tools.x402.discovery import score_endpoint

    score, reasons = score_endpoint({"status": None, "error": "timeout"})

    assert score == 0
    assert any("did not answer" in r for r in reasons)


def test_score_open_endpoint_scores_one_and_says_it_is_not_paid():
    from tools.x402.discovery import score_endpoint

    score, reasons = score_endpoint({"status": 200, "error": None})

    assert score == 1
    assert any("not a paid endpoint" in r for r in reasons)


def test_score_402_without_routing_is_not_payable_in_practice():
    """A 402 that never names asset+network+payTo cannot actually be paid."""
    from tools.x402.discovery import score_endpoint

    row = {"status": 402, "error": None, "challenge_parseable": True,
           "price_usd": None, "asset": None, "network": None, "pay_to": None}
    score, reasons = score_endpoint(row)

    assert score == 3
    assert any("no price" in r for r in reasons)
    assert any("routing" in r for r in reasons)


# --------------------------------------------------------------------------
# probe
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_parses_a_body_carried_challenge():
    from tools.x402.discovery import probe_endpoint

    url = "https://pay.test/a2a/rpc"
    r = await probe_endpoint(url, fetch=fake_fetch({url: FakeResponse(402, CHALLENGE)}),
                             validator=_no_validate())

    assert r["status"] == 402
    assert r["challenge_parseable"] is True
    assert r["price_usd"] == pytest.approx(0.01)
    assert r["network"] == "base"
    assert r["pay_to"] == "0xTreasury"
    assert r["score"] == 5
    assert [a["network"] for a in r["accepts"]] == ["base", "base-sepolia"]


@pytest.mark.asyncio
async def test_probe_reports_an_open_endpoint_honestly():
    from tools.x402.discovery import probe_endpoint

    url = "https://free.test/data"
    r = await probe_endpoint(url, fetch=fake_fetch({url: FakeResponse(200, "hello")}),
                             validator=_no_validate())

    assert r["status"] == 200
    assert r["challenge_parseable"] is False
    assert r["price_usd"] is None
    assert r["score"] == 1


@pytest.mark.asyncio
async def test_probe_network_error_is_reported_not_raised():
    from tools.x402.discovery import probe_endpoint

    url = "https://dead.test/x"
    r = await probe_endpoint(url, fetch=fake_fetch({url: TimeoutError("timed out")}),
                             validator=_no_validate())

    assert r["status"] is None
    assert "TimeoutError" in r["error"]
    assert r["score"] == 0


@pytest.mark.asyncio
async def test_probe_post_only_surface_needs_the_method():
    """JSON-RPC / A2A surfaces only reveal their 402 on POST."""
    from tools.x402.discovery import probe_endpoint

    url = "https://rpc.test/"
    seen = {}

    async def _fetch(u, *, method="GET", body=None, timeout=None):
        seen["method"], seen["body"] = method, body
        return FakeResponse(402, CHALLENGE) if method == "POST" else FakeResponse(405)

    r = await probe_endpoint(url, method="post", body='{"jsonrpc":"2.0"}',
                             fetch=_fetch, validator=_no_validate())

    assert seen["method"] == "POST"
    assert seen["body"] == '{"jsonrpc":"2.0"}'
    assert r["status"] == 402 and r["score"] == 5


@pytest.mark.asyncio
async def test_probe_never_sends_a_payment_header():
    """The one invariant that must never regress: discovery does not pay."""
    from tools.x402.discovery import probe_endpoint

    url = "https://pay.test/x"
    captured = {}

    async def _fetch(u, *, method="GET", body=None, timeout=None, headers=None):
        captured["headers"] = headers or {}
        return FakeResponse(402, CHALLENGE)

    await probe_endpoint(url, fetch=_fetch, validator=_no_validate())

    assert not any(k.upper().startswith("X-PAYMENT") for k in captured.get("headers", {}))


@pytest.mark.asyncio
async def test_probe_refuses_a_blocked_url():
    """SSRF: the agent supplies these URLs, so cloud metadata / RFC1918 stay shut."""
    from tools.x402.discovery import probe_endpoint

    class _Blocking:
        def validate_and_resolve(self, url):
            return False, "private address", None

    async def _never(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("fetch ran despite a blocked URL")

    r = await probe_endpoint("http://169.254.169.254/latest/meta-data/",
                             fetch=_never, validator=_Blocking())

    assert r["status"] is None
    assert "blocked" in r["error"].lower()
    assert r["score"] == 0


@pytest.mark.asyncio
async def test_validate_returns_the_validator_cleared_ip_for_pinning():
    """DNS-rebind defense: _validate must surface the resolved IP so the fetch
    pins the socket to it instead of re-resolving (which an attacker's TTL-0 DNS
    could point at 169.254.169.254 after the check passed)."""
    from tools.x402.discovery import _validate

    class _V:
        def validate_and_resolve(self, url):
            return True, None, "93.184.216.34"

    refusal, pinned = await _validate("https://example.test/x", _V())
    assert refusal is None
    assert pinned == "93.184.216.34"

    class _Blocked:
        def validate_and_resolve(self, url):
            return False, "private address", None

    refusal, pinned = await _validate("http://10.0.0.1/", _Blocked())
    assert refusal and "blocked" in refusal.lower()
    assert pinned is None


# --------------------------------------------------------------------------
# sweep
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sweep_returns_a_scored_ledger():
    from tools.x402.discovery import sweep_endpoints

    paid, free, dead = "https://a.test/p", "https://b.test/f", "https://c.test/d"
    fetch = fake_fetch({
        paid: FakeResponse(402, CHALLENGE),
        free: FakeResponse(200, "ok"),
        dead: ConnectionError("refused"),
    })

    ledger = await sweep_endpoints(
        [{"service": "A", "url": paid}, {"service": "B", "url": free},
         {"service": "C", "url": dead}],
        fetch=fetch, validator=_no_validate())

    assert ledger["swept"] == 3
    assert ledger["outcome_distribution"] == {
        "http_402": 1, "http_200_open": 1, "dead": 1}
    by_service = {e["service"]: e for e in ledger["endpoints"]}
    assert by_service["A"]["score"] == 5
    assert by_service["A"]["price_usd"] == pytest.approx(0.01)
    assert by_service["C"]["score"] == 0
    assert ledger["payable"] == 1


@pytest.mark.asyncio
async def test_sweep_accepts_bare_url_strings():
    from tools.x402.discovery import sweep_endpoints

    url = "https://a.test/p"
    ledger = await sweep_endpoints([url],
                                   fetch=fake_fetch({url: FakeResponse(402, CHALLENGE)}),
                                   validator=_no_validate())

    assert ledger["endpoints"][0]["service"] == url
    assert ledger["endpoints"][0]["score"] == 5


@pytest.mark.asyncio
async def test_sweep_marks_a_402_that_discloses_nothing_as_unpayable():
    from tools.x402.discovery import sweep_endpoints

    url = "https://vague.test/p"
    ledger = await sweep_endpoints([url],
                                   fetch=fake_fetch({url: FakeResponse(402, BARE_402)}),
                                   validator=_no_validate())

    row = ledger["endpoints"][0]
    assert row["status"] == 402
    assert row["price_usd"] is None
    assert row["score"] < 5
    assert ledger["payable"] == 0, "a 402 with no price or routing is not payable"


@pytest.mark.asyncio
async def test_sweep_is_bounded():
    from tools.x402.discovery import sweep_endpoints, MAX_SWEEP_TARGETS

    too_many = [f"https://t{i}.test/" for i in range(MAX_SWEEP_TARGETS + 5)]

    with pytest.raises(ValueError, match="at most"):
        await sweep_endpoints(too_many, fetch=fake_fetch({}), validator=_no_validate())
