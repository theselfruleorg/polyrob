"""G-20 — the public, anon-allowed invoice endpoints
(GET /api/x402/requests/{id}, POST /api/x402/requests/{id}/pay) let any third
party read invoice metadata or attempt settlement by guessing an
``inv_<12hex>`` id, with no rate limiting at the endpoint level. This adds a
per-IP throttle reusing the EXISTING generic sliding-window limiter
(``tools/mcp/rate_limit.py::MCPExecRateLimiter``) — no new rate-limit
mechanism. Uses ``TestClient`` (a real ASGI request carries a resolvable
``request.client``) so the throttle actually engages, unlike the direct
function-call tests in ``test_x402_pay_endpoint.py``.

G-20 FOLLOW-UP (Critical regression, reproduced live): the limiter was keyed
on ``get_client_ip``, which returns a client-supplied ``X-Forwarded-For``
VERBATIM — spoofable. Two live-reproduced attacks:
  - EVASION: rotate a different fake X-Forwarded-For per request -> every
    request gets a fresh bucket, the cap never engages.
  - BUCKET-POISON DoS: spoof a victim payer's real IP in X-Forwarded-For ->
    burn the victim's budget -> the victim's own genuine requests then 429.
The classes below (``TestSpoofRegressions``) pin the fix: the limiter is now
keyed on ``get_trusted_client_ip`` (trusted-proxy-aware; see
api/dependencies.py), which ignores X-Forwarded-For entirely from any peer
this deployment doesn't explicitly trust.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tools.mcp.rate_limit import MCPExecRateLimiter

from api import x402_endpoints as ep

ROW = {"status": "pending", "amount_usd": 5.0, "chain": "base",
       "recipient": "0xt", "purpose": "svc"}


def _asset_cfg():
    return SimpleNamespace(decimals=6, address="0xasset", eip712_name="USDC", eip712_version="2")


def _app(client=("testclient", 50000)):
    """``client`` overrides the ASGI-scope TCP peer (host, port) TestClient
    presents as ``request.client`` — lets tests simulate a specific real
    (un-spoofable) network peer, e.g. a trusted nginx-loopback front-end
    (``("127.0.0.1", 0)``) vs. an arbitrary untrusted direct connection."""
    app = FastAPI()
    app.include_router(ep.router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False, client=client)


def _small_limiter(max_calls, window_seconds=60):
    return MCPExecRateLimiter(max_calls=max_calls, window_seconds=window_seconds)


def test_challenge_endpoint_blocks_after_per_ip_limit(monkeypatch):
    monkeypatch.setattr(ep, "_PUBLIC_INVOICE_RATE_LIMITER", _small_limiter(2))
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    client = _app()
    with patch("modules.x402.invoicing.get_payment_request",
               new=AsyncMock(return_value=ROW)), \
         patch.object(ep, "_invoice_asset_cfg", lambda net: _asset_cfg()):
        r1 = client.get("/api/x402/requests/inv_a")
        r2 = client.get("/api/x402/requests/inv_b")
        r3 = client.get("/api/x402/requests/inv_c")

    assert r1.status_code == 402  # under budget: normal pending-invoice challenge
    assert r2.status_code == 402
    assert r3.status_code == 429  # 3rd call from the same IP within the window
    assert "Retry-After" in r3.headers


def test_challenge_endpoint_low_rate_access_not_limited(monkeypatch):
    # Default-sized limiter (X402_PUBLIC_RATE_PER_WINDOW, default 20) — a
    # legit payer polling the SAME invoice a handful of times must not trip.
    monkeypatch.setattr(ep, "_PUBLIC_INVOICE_RATE_LIMITER", _small_limiter(20))
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    client = _app()
    with patch("modules.x402.invoicing.get_payment_request",
               new=AsyncMock(return_value=ROW)), \
         patch.object(ep, "_invoice_asset_cfg", lambda net: _asset_cfg()):
        for _ in range(3):
            r = client.get("/api/x402/requests/inv_same")
            assert r.status_code == 402


def test_pay_endpoint_blocks_after_per_ip_limit(monkeypatch):
    monkeypatch.setattr(ep, "_PUBLIC_INVOICE_RATE_LIMITER", _small_limiter(1))
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    client = _app()
    with patch("modules.x402.invoicing.get_payment_request",
               new=AsyncMock(return_value=ROW)):
        r1 = client.post("/api/x402/requests/inv_a/pay")  # no X-PAYMENT -> 402, but under budget
        r2 = client.post("/api/x402/requests/inv_b/pay")

    assert r1.status_code == 402
    assert r2.status_code == 429
    assert "Retry-After" in r2.headers


def test_challenge_and_pay_have_independent_budgets(monkeypatch):
    monkeypatch.setattr(ep, "_PUBLIC_INVOICE_RATE_LIMITER", _small_limiter(1))
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    client = _app()
    with patch("modules.x402.invoicing.get_payment_request",
               new=AsyncMock(return_value=ROW)), \
         patch.object(ep, "_invoice_asset_cfg", lambda net: _asset_cfg()):
        r_challenge = client.get("/api/x402/requests/inv_a")
        r_pay = client.post("/api/x402/requests/inv_a/pay")

    assert r_challenge.status_code == 402
    # pay's own budget is untouched by the earlier challenge call.
    assert r_pay.status_code == 402


@pytest.mark.asyncio
async def test_direct_function_call_without_request_is_not_rate_limited(monkeypatch):
    """Back-compat: existing tests (test_x402_pay_endpoint.py) call the route
    handlers directly with no Request object. That path must stay unthrottled
    (there's no client identity to key on) so those tests keep passing."""
    monkeypatch.setattr(ep, "_PUBLIC_INVOICE_RATE_LIMITER", _small_limiter(1))
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    with patch("modules.x402.invoicing.get_payment_request",
               new=AsyncMock(return_value={"status": "completed"})):
        for _ in range(5):
            resp = await ep.get_invoice_challenge("inv_x")
            assert resp.status_code == 200


class TestSpoofRegressions:
    """Critical regression, reproduced live against the ORIGINAL G-20 fix
    (keyed on ``get_client_ip``, which trusts client-supplied
    X-Forwarded-For). These pin the corrected behavior: the limiter key must
    be un-spoofable, so it can only ever come from something the caller's
    real network identity determines.

    To verify these actually catch the regression: temporarily change
    ``_enforce_public_invoice_rate_limit`` back to
    ``get_client_ip(request)`` (the pre-fix code) and re-run this class —
    ``test_spoof_evasion_...`` and ``test_spoof_poison_...`` both fail against
    that version (6/6 requests succeed instead of being capped, and the
    victim's genuine request 429s instead of succeeding).
    """

    def test_spoof_evasion_regression_rotating_xff_same_untrusted_peer_still_capped(
        self, monkeypatch
    ):
        """Attacker rotates a DIFFERENT spoofed X-Forwarded-For on every
        request from the SAME real (untrusted, direct) TCP connection. The
        cap must still engage — the un-spoofable peer is what's keyed on, not
        the attacker-controlled header."""
        monkeypatch.setattr(ep, "_PUBLIC_INVOICE_RATE_LIMITER", _small_limiter(2))
        monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
        # An arbitrary direct/unknown peer — NOT a trusted proxy.
        client = _app(client=("6.6.6.6", 0))
        statuses = []
        with patch("modules.x402.invoicing.get_payment_request",
                   new=AsyncMock(return_value=ROW)), \
             patch.object(ep, "_invoice_asset_cfg", lambda net: _asset_cfg()):
            for i in range(6):
                r = client.get(
                    f"/api/x402/requests/inv_{i}",
                    headers={"X-Forwarded-For": f"203.0.113.{i}"},  # different spoof every time
                )
                statuses.append(r.status_code)

        assert statuses[:2] == [402, 402]
        assert statuses[2:] == [429, 429, 429, 429], (
            f"attacker evaded the per-peer cap by rotating X-Forwarded-For: {statuses}"
        )

    def test_spoof_poison_regression_untrusted_attacker_cannot_burn_victims_bucket(
        self, monkeypatch
    ):
        """Attacker (a genuinely different, untrusted real peer) spoofs the
        victim's real IP in X-Forwarded-For, trying to exhaust the victim's
        rate-limit budget. The victim's OWN later request (arriving through
        the trusted nginx-loopback front-end with a legitimate, nginx-set
        X-Forwarded-For) must NOT be affected."""
        monkeypatch.setattr(ep, "_PUBLIC_INVOICE_RATE_LIMITER", _small_limiter(1))
        monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
        victim_ip = "198.51.100.7"

        with patch("modules.x402.invoicing.get_payment_request",
                   new=AsyncMock(return_value=ROW)), \
             patch.object(ep, "_invoice_asset_cfg", lambda net: _asset_cfg()):
            # Attacker connects directly (untrusted peer) and forges the
            # victim's IP in X-Forwarded-For.
            attacker_client = _app(client=("6.6.6.6", 0))
            r_attack = attacker_client.get(
                "/api/x402/requests/inv_attack",
                headers={"X-Forwarded-For": victim_ip},
            )

            # Victim connects through the trusted nginx-loopback front-end;
            # nginx sets X-Forwarded-For to the victim's real IP.
            victim_client = _app(client=("127.0.0.1", 0))
            r_victim = victim_client.get(
                "/api/x402/requests/inv_victim",
                headers={"X-Forwarded-For": victim_ip},
            )

        assert r_attack.status_code == 402  # attacker consumed their OWN bucket
        assert r_victim.status_code == 402, (
            "victim's genuine request was 429'd — attacker poisoned the victim's "
            "bucket by spoofing their IP from an untrusted peer"
        )

    def test_trusted_proxy_walks_past_forged_leftmost_entry_to_real_attacker_hop(
        self, monkeypatch
    ):
        """The stronger form of the poisoning attack: attacker ALSO goes
        through the trusted nginx front-end (so the peer IS trusted), forging
        a LEFTMOST victim-claim entry — but nginx APPENDS the attacker's own
        real observed address as the RIGHTMOST hop. Resolution must walk from
        the right and land on the attacker's real address, never the forged
        victim claim, so the victim's separate single-hop bucket is untouched."""
        monkeypatch.setattr(ep, "_PUBLIC_INVOICE_RATE_LIMITER", _small_limiter(1))
        monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
        victim_ip = "198.51.100.7"
        attacker_real_ip = "6.6.6.6"

        with patch("modules.x402.invoicing.get_payment_request",
                   new=AsyncMock(return_value=ROW)), \
             patch.object(ep, "_invoice_asset_cfg", lambda net: _asset_cfg()):
            proxied_client = _app(client=("127.0.0.1", 0))
            r_attack = proxied_client.get(
                "/api/x402/requests/inv_attack2",
                headers={"X-Forwarded-For": f"{victim_ip}, {attacker_real_ip}"},
            )
            r_victim = proxied_client.get(
                "/api/x402/requests/inv_victim2",
                headers={"X-Forwarded-For": victim_ip},
            )

        assert r_attack.status_code == 402  # attacker consumed their OWN bucket (attacker_real_ip)
        assert r_victim.status_code == 402, "victim's single-hop bucket must be independent"

    def test_two_real_clients_behind_trusted_proxy_get_independent_buckets(self, monkeypatch):
        monkeypatch.setattr(ep, "_PUBLIC_INVOICE_RATE_LIMITER", _small_limiter(1))
        monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
        client = _app(client=("127.0.0.1", 0))

        with patch("modules.x402.invoicing.get_payment_request",
                   new=AsyncMock(return_value=ROW)), \
             patch.object(ep, "_invoice_asset_cfg", lambda net: _asset_cfg()):
            r1 = client.get("/api/x402/requests/inv_c1", headers={"X-Forwarded-For": "9.9.9.9"})
            r2 = client.get("/api/x402/requests/inv_c2", headers={"X-Forwarded-For": "8.8.8.8"})

        assert r1.status_code == 402
        assert r2.status_code == 402

    def test_legit_payer_under_cap_via_trusted_proxy_not_429(self, monkeypatch):
        monkeypatch.setattr(ep, "_PUBLIC_INVOICE_RATE_LIMITER", _small_limiter(20))
        monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
        client = _app(client=("127.0.0.1", 0))

        with patch("modules.x402.invoicing.get_payment_request",
                   new=AsyncMock(return_value=ROW)), \
             patch.object(ep, "_invoice_asset_cfg", lambda net: _asset_cfg()):
            for _ in range(3):
                r = client.get(
                    "/api/x402/requests/inv_same",
                    headers={"X-Forwarded-For": "203.0.113.20"},
                )
                assert r.status_code == 402
