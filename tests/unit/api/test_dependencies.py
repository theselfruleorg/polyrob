"""Unit tests for api/dependencies.py — TDD-first (RED before implementation).

Tests assert exact behavior for each consolidated dependency:
- get_user_id: canonical identity extraction
- get_client_ip: consistent default, X-Forwarded-For precedence
- get_user_strict: STRICT auth (rejects api_user / authenticated_api_user)
- get_user_permissive: PERMISSIVE auth (accepts x402, API-key fallback)
- resolve_orchestrator: calls clean_session_id + guard_remote; REMOTE → 409
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# Helpers to build a minimal fake Request.
# We only touch .state, .headers, and .client — nothing else is needed.
# ---------------------------------------------------------------------------

class _FakeState:
    """Bare-minimum request.state stand-in."""


class _FakeClient:
    def __init__(self, host: str = "1.2.3.4"):
        self.host = host


class _FakeHeaders(dict):
    """dict subclass so .get() works like real headers."""


def _make_request(
    *,
    state_attrs: dict = None,
    headers: dict = None,
    client_host: str = "1.2.3.4",
    no_client: bool = False,
):
    """Construct a minimal fake FastAPI Request."""
    req = MagicMock()
    req.state = _FakeState()
    req.headers = _FakeHeaders(headers or {})
    req.client = None if no_client else _FakeClient(client_host)

    for key, value in (state_attrs or {}).items():
        setattr(req.state, key, value)

    return req


# ===========================================================================
# get_user_id
# ===========================================================================

class TestGetUserId:
    def test_returns_user_id_from_state(self):
        from api.dependencies import get_user_id
        req = _make_request(state_attrs={"user_id": "usr_abc"})
        assert get_user_id(req) == "usr_abc"

    def test_raises_401_when_missing(self):
        from api.dependencies import get_user_id
        req = _make_request()  # no user_id set
        with pytest.raises(HTTPException) as ei:
            get_user_id(req)
        assert ei.value.status_code == 401

    def test_raises_401_when_none(self):
        from api.dependencies import get_user_id
        req = _make_request(state_attrs={"user_id": None})
        with pytest.raises(HTTPException) as ei:
            get_user_id(req)
        assert ei.value.status_code == 401


# ===========================================================================
# get_client_ip
# ===========================================================================

class TestGetClientIp:
    def test_xforwardedfor_takes_precedence(self):
        from api.dependencies import get_client_ip
        req = _make_request(
            headers={"X-Forwarded-For": "10.0.0.1, 192.168.1.1"},
            client_host="1.2.3.4",
        )
        assert get_client_ip(req) == "10.0.0.1"

    def test_falls_back_to_client_host(self):
        from api.dependencies import get_client_ip
        req = _make_request(client_host="5.6.7.8")
        assert get_client_ip(req) == "5.6.7.8"

    def test_default_when_no_client(self):
        from api.dependencies import get_client_ip
        req = _make_request(no_client=True)
        # Must return a string — the canonical default is "unknown"
        result = get_client_ip(req)
        assert isinstance(result, str)
        assert result == "unknown"

    def test_consistent_default_across_calls(self):
        """Same absent-client scenario must produce identical output every time."""
        from api.dependencies import get_client_ip
        req1 = _make_request(no_client=True)
        req2 = _make_request(no_client=True)
        assert get_client_ip(req1) == get_client_ip(req2)

    def test_strips_whitespace_from_forwarded(self):
        from api.dependencies import get_client_ip
        req = _make_request(headers={"X-Forwarded-For": "  203.0.113.5 , 10.0.0.1"})
        assert get_client_ip(req) == "203.0.113.5"


# ===========================================================================
# get_trusted_client_ip — G-20 rate-limit-key regression fix.
#
# Unlike get_client_ip (which trusts a client-supplied X-Forwarded-For
# verbatim), this helper must never let request headers alone determine the
# resolved identity: only a peer this deployment actually trusts (loopback by
# default, plus X402_TRUSTED_PROXIES) may contribute X-Forwarded-For content.
# ===========================================================================

class TestGetTrustedClientIp:
    def test_untrusted_peer_ignores_spoofed_xff(self):
        """A direct/unknown connection's X-Forwarded-For is attacker-supplied
        and must be completely ignored — the real TCP peer is the identity."""
        from api.dependencies import get_trusted_client_ip
        req = _make_request(
            headers={"X-Forwarded-For": "9.9.9.9"},  # spoofed
            client_host="1.2.3.4",  # real, un-spoofable peer
        )
        assert get_trusted_client_ip(req) == "1.2.3.4"

    def test_untrusted_peer_with_no_xff_uses_peer(self):
        from api.dependencies import get_trusted_client_ip
        req = _make_request(client_host="5.6.7.8")
        assert get_trusted_client_ip(req) == "5.6.7.8"

    def test_trusted_loopback_peer_single_hop_xff_uses_real_client(self):
        """Peer is the standard nginx-loopback deployment shape: XFF carries
        exactly the one real client hop nginx observed and appended."""
        from api.dependencies import get_trusted_client_ip
        req = _make_request(
            headers={"X-Forwarded-For": "9.9.9.9"},
            client_host="127.0.0.1",
        )
        assert get_trusted_client_ip(req) == "9.9.9.9"

    def test_trusted_peer_ipv6_loopback_also_trusted(self):
        from api.dependencies import get_trusted_client_ip
        req = _make_request(
            headers={"X-Forwarded-For": "9.9.9.9"},
            client_host="::1",
        )
        assert get_trusted_client_ip(req) == "9.9.9.9"

    def test_trusted_peer_walks_past_left_spoofed_entry_to_real_rightmost_hop(self):
        """The bucket-poison attack: attacker forges a leftmost entry claiming
        to be the victim, but nginx APPENDS the attacker's real observed
        address as the rightmost hop. Resolution must walk from the right and
        return the attacker's real address, never the forged victim claim."""
        from api.dependencies import get_trusted_client_ip
        req = _make_request(
            headers={"X-Forwarded-For": "203.0.113.9, 6.6.6.6"},  # victim(forged), attacker(real)
            client_host="127.0.0.1",
        )
        assert get_trusted_client_ip(req) == "6.6.6.6"

    def test_trusted_peer_skips_multiple_trusted_hops_in_chain(self):
        """A chained-proxy deployment (e.g. CDN -> nginx): both intermediate
        hop addresses are configured trusted via X402_TRUSTED_PROXIES, so the
        walk must skip past ALL of them to reach the genuine client entry."""
        from api.dependencies import get_trusted_client_ip
        req = _make_request(
            headers={"X-Forwarded-For": "9.9.9.9, 10.0.0.5"},
            client_host="127.0.0.1",
        )
        result = get_trusted_client_ip(
            req, trusted_proxies=frozenset({"127.0.0.1", "::1", "10.0.0.5"})
        )
        assert result == "9.9.9.9"

    def test_env_x402_trusted_proxies_extends_default_set(self, monkeypatch):
        from api.dependencies import get_trusted_client_ip
        monkeypatch.setenv("X402_TRUSTED_PROXIES", "10.0.0.5, 10.0.0.6")
        req = _make_request(
            headers={"X-Forwarded-For": "9.9.9.9, 10.0.0.5"},
            client_host="127.0.0.1",
        )
        assert get_trusted_client_ip(req) == "9.9.9.9"

    def test_trusted_peer_missing_xff_falls_back_to_peer(self):
        from api.dependencies import get_trusted_client_ip
        req = _make_request(client_host="127.0.0.1")
        assert get_trusted_client_ip(req) == "127.0.0.1"

    def test_trusted_peer_all_trusted_hops_falls_back_to_peer(self):
        """Degenerate case: XFF contains only trusted-proxy addresses (no
        genuine client entry survived) — never crash, fall back to the peer."""
        from api.dependencies import get_trusted_client_ip
        req = _make_request(
            headers={"X-Forwarded-For": "127.0.0.1"},
            client_host="127.0.0.1",
        )
        assert get_trusted_client_ip(req) == "127.0.0.1"

    def test_two_real_clients_behind_trusted_proxy_get_independent_identities(self):
        """Same proxy peer, two different genuine XFF-reported clients ->
        two different resolved identities (independent rate-limit buckets)."""
        from api.dependencies import get_trusted_client_ip
        req_a = _make_request(headers={"X-Forwarded-For": "9.9.9.9"}, client_host="127.0.0.1")
        req_b = _make_request(headers={"X-Forwarded-For": "8.8.8.8"}, client_host="127.0.0.1")
        assert get_trusted_client_ip(req_a) != get_trusted_client_ip(req_b)

    def test_no_client_returns_none(self):
        from api.dependencies import get_trusted_client_ip
        req = _make_request(no_client=True)
        assert get_trusted_client_ip(req) is None


# ===========================================================================
# get_user_strict  (mirrors payment_endpoints.get_authenticated_user)
# ===========================================================================

class TestGetUserStrict:
    @pytest.mark.asyncio
    async def test_returns_real_user_id(self):
        from api.dependencies import get_user_strict
        req = _make_request(state_attrs={"user_id": "usr_real"})
        result = await get_user_strict(req)
        assert result == "usr_real"

    @pytest.mark.asyncio
    async def test_rejects_api_user(self):
        """api_user is a synthetic fallback — strict policy must reject it."""
        from api.dependencies import get_user_strict
        req = _make_request(state_attrs={"user_id": "api_user"})
        with pytest.raises(HTTPException) as ei:
            await get_user_strict(req)
        assert ei.value.status_code == 401

    @pytest.mark.asyncio
    async def test_rejects_authenticated_api_user(self):
        """authenticated_api_user is the API-key fallback — strict policy must reject it."""
        from api.dependencies import get_user_strict
        req = _make_request(state_attrs={"user_id": "authenticated_api_user"})
        with pytest.raises(HTTPException) as ei:
            await get_user_strict(req)
        assert ei.value.status_code == 401

    @pytest.mark.asyncio
    async def test_rejects_missing_user_id(self):
        from api.dependencies import get_user_strict
        req = _make_request()  # no user_id attribute
        with pytest.raises(HTTPException) as ei:
            await get_user_strict(req)
        assert ei.value.status_code == 401

    @pytest.mark.asyncio
    async def test_rejects_none_user_id(self):
        from api.dependencies import get_user_strict
        req = _make_request(state_attrs={"user_id": None})
        with pytest.raises(HTTPException) as ei:
            await get_user_strict(req)
        assert ei.value.status_code == 401


# ===========================================================================
# get_user_permissive  (mirrors a2a/endpoints.get_authenticated_user)
# ===========================================================================

class TestGetUserPermissive:
    @pytest.mark.asyncio
    async def test_returns_real_user_id_from_jwt(self):
        from api.dependencies import get_user_permissive
        req = _make_request(state_attrs={"user_id": "usr_jwt", "authenticated": True})
        result = await get_user_permissive(req)
        assert result == "usr_jwt"

    @pytest.mark.asyncio
    async def test_accepts_x402_payment_with_user_id(self):
        """x402 payment path with user_id already set by middleware."""
        from api.dependencies import get_user_permissive
        req = _make_request(state_attrs={
            "payment_method": "x402",
            "user_id": "usr_x402",
        })
        result = await get_user_permissive(req)
        assert result == "usr_x402"

    @pytest.mark.asyncio
    async def test_x402_wallet_fallback(self):
        """x402 path without user_id falls back to wallet-derived ID.

        ``generate_user_id_from_wallet`` is imported lazily inside the function
        body via ``from core.identity import generate_user_id_from_wallet``
        (api/dependencies.py), so we patch it at its canonical definition site
        (``core.identity``) — the ``modules.x402.middleware`` re-export is not on
        this code path, so patching there is a no-op.
        """
        from api.dependencies import get_user_permissive

        req = _make_request(state_attrs={
            "payment_method": "x402",
            "payer_address": "0xDEAD",
        })

        with patch(
            "core.identity.generate_user_id_from_wallet",
            return_value="usr_wallet_derived",
        ):
            result = await get_user_permissive(req)

        assert result == "usr_wallet_derived"

    @pytest.mark.asyncio
    async def test_x402_ultimate_fallback(self):
        """x402 path with no user_id and no payer_address → 'x402_user'."""
        from api.dependencies import get_user_permissive
        req = _make_request(state_attrs={"payment_method": "x402"})
        result = await get_user_permissive(req)
        assert result == "x402_user"

    @pytest.mark.asyncio
    async def test_accepts_authenticated_api_user(self):
        """Permissive policy ACCEPTS authenticated_api_user (unlike strict)."""
        from api.dependencies import get_user_permissive
        req = _make_request(state_attrs={
            "authenticated": True,
            "user_id": "authenticated_api_user",
        })
        result = await get_user_permissive(req)
        assert result == "authenticated_api_user"

    @pytest.mark.asyncio
    async def test_skips_api_user_placeholder_from_jwt(self):
        """api_user is NOT a real identity — permissive policy skips it and
        falls through to the authenticated-API-key check."""
        from api.dependencies import get_user_permissive
        req = _make_request(state_attrs={
            "user_id": "api_user",
            "authenticated": True,
        })
        # When user_id == 'api_user', the JWT path is bypassed and the
        # authenticated-state path runs, returning 'api_user' from state.user_id.
        # This mirrors the exact a2a/endpoints.py logic verbatim.
        result = await get_user_permissive(req)
        assert result == "api_user"

    @pytest.mark.asyncio
    async def test_raises_401_when_unauthenticated(self):
        """No payment, no JWT, no API key → 401."""
        from api.dependencies import get_user_permissive
        req = _make_request()  # empty state
        with pytest.raises(HTTPException) as ei:
            await get_user_permissive(req)
        assert ei.value.status_code == 401


# ===========================================================================
# resolve_orchestrator
# ===========================================================================

class TestResolveOrchestrator:
    """resolve_orchestrator must:
    1. Call pm().clean_session_id() to normalise the ID.
    2. Call guard_remote() to catch REMOTE sessions before get_orchestrator().
    3. Call agent.get_orchestrator() with the cleaned ID.
    4. Raise 409 (not 404) for a REMOTE session.
    5. Return the orchestrator object for LOCAL.
    6. Return None for MISSING.
    """

    def _make_agent(self, *, route_session_result=None, orchestrator=None):
        agent = MagicMock()
        agent.get_orchestrator.return_value = orchestrator
        agent.route_session.return_value = route_session_result
        return agent

    def _mock_pm(self, cleaned: str = "clean_sid"):
        pm_mock = MagicMock()
        pm_mock.clean_session_id.return_value = cleaned
        return pm_mock

    @pytest.mark.asyncio
    async def test_calls_clean_session_id(self):
        from api.dependencies import resolve_orchestrator
        from agents.task.session_route import SessionRoute, LOCAL

        agent = self._make_agent(
            route_session_result=SessionRoute(status=LOCAL, orchestrator=object()),
            orchestrator=object(),
        )
        pm_mock = self._mock_pm("cleaned_sid")

        with patch("api.dependencies.pm", return_value=pm_mock):
            with patch("api.dependencies.guard_remote"):
                await resolve_orchestrator("raw_sid", agent)

        pm_mock.clean_session_id.assert_called_once_with("raw_sid")

    @pytest.mark.asyncio
    async def test_calls_guard_remote_with_cleaned_id(self):
        from api.dependencies import resolve_orchestrator
        from agents.task.session_route import SessionRoute, LOCAL

        orch = object()
        agent = self._make_agent(
            route_session_result=SessionRoute(status=LOCAL, orchestrator=orch),
            orchestrator=orch,
        )
        pm_mock = self._mock_pm("cln")

        with patch("api.dependencies.pm", return_value=pm_mock):
            with patch("api.dependencies.guard_remote") as gr_mock:
                await resolve_orchestrator("raw", agent)

        gr_mock.assert_called_once_with(agent, "cln")

    @pytest.mark.asyncio
    async def test_remote_session_raises_409(self):
        """guard_remote propagates the 409 — resolve_orchestrator must NOT catch it."""
        from api.dependencies import resolve_orchestrator

        pm_mock = self._mock_pm("cln")
        agent = MagicMock()

        with patch("api.dependencies.pm", return_value=pm_mock):
            with patch(
                "api.dependencies.guard_remote",
                side_effect=HTTPException(
                    status_code=409,
                    detail={"session_id": "cln", "owner_pid": 99},
                    headers={"Retry-After": "1"},
                ),
            ):
                with pytest.raises(HTTPException) as ei:
                    await resolve_orchestrator("raw", agent)

        assert ei.value.status_code == 409
        assert ei.value.detail["owner_pid"] == 99

    @pytest.mark.asyncio
    async def test_local_session_returns_orchestrator(self):
        from api.dependencies import resolve_orchestrator
        from agents.task.session_route import SessionRoute, LOCAL

        orch = object()
        agent = self._make_agent(
            route_session_result=SessionRoute(status=LOCAL, orchestrator=orch),
            orchestrator=orch,
        )
        pm_mock = self._mock_pm("cln")

        with patch("api.dependencies.pm", return_value=pm_mock):
            with patch("api.dependencies.guard_remote"):
                result = await resolve_orchestrator("raw", agent)

        agent.get_orchestrator.assert_called_once_with("cln")
        assert result is orch

    @pytest.mark.asyncio
    async def test_missing_session_returns_none(self):
        from api.dependencies import resolve_orchestrator

        agent = MagicMock()
        agent.get_orchestrator.return_value = None
        pm_mock = self._mock_pm("cln")

        with patch("api.dependencies.pm", return_value=pm_mock):
            with patch("api.dependencies.guard_remote"):
                result = await resolve_orchestrator("raw", agent)

        assert result is None
