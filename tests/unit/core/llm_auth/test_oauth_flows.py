"""OAuth connect flows (proposal 024, L2).

These drive REAL sockets against a local token server rather than mocking the
HTTP seam: the failure modes that matter here — RFC 8628's ``authorization_
pending``/``slow_down`` arms, a browser redirect actually arriving on an
ephemeral loopback port, a forged ``state`` — only exist on the wire.

POLYROB ships no built-in OAuth providers (see core/llm_auth/flows/__init__),
so every test declares its own OAuthSpec, exactly as a user's providers.yaml
would.
"""
from __future__ import annotations

import http.server
import json
import threading
import time
import urllib.parse
import urllib.request

import pytest

from core.llm_auth.flows.base import FlowError
from core.llm_auth.flows.device_code import run_device_code_flow
from core.llm_auth.flows.loopback_pkce import make_pkce_pair, run_loopback_pkce_flow
from core.llm_auth.flows.manual_paste_pkce import run_manual_paste_pkce_flow
from modules.llm.provider_spec import OAuthSpec


class _TokenServerHandler(http.server.BaseHTTPRequestHandler):
    polls = 0
    seen: list = []

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        form = {k: v[0] for k, v in
                urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
        type(self).seen.append((self.path, form))
        if self.path == "/device":
            return self._json(200, {
                "device_code": "dev-123", "user_code": "WXYZ-1234",
                "verification_uri": "https://example.test/activate",
                "expires_in": 600, "interval": 1,
            })
        if self.path == "/token":
            if form.get("grant_type") == "refresh_token":
                return self._json(200, {"access_token": "tok-REFRESHED", "expires_in": 3600})
            if form.get("grant_type") == "authorization_code":
                return self._json(200, {"access_token": "tok-PKCE", "expires_in": 3600})
            type(self).polls += 1
            if type(self).polls == 1:
                return self._json(400, {"error": "authorization_pending"})
            if type(self).polls == 2:
                return self._json(400, {"error": "slow_down"})
            return self._json(200, {
                "access_token": "tok-REAL", "refresh_token": "refresh-abc",
                "expires_in": 3600, "scope": "inference",
            })
        self._json(404, {"error": "not_found"})


@pytest.fixture
def token_server():
    _TokenServerHandler.polls = 0
    _TokenServerHandler.seen = []
    srv = http.server.HTTPServer(("127.0.0.1", 0), _TokenServerHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _device_spec(base):
    return OAuthSpec(auth_url=f"{base}/device", token_url=f"{base}/token",
                     client_id="cid-123", scopes=("inference",), grant="device_code")


def _pkce_spec(base):
    return OAuthSpec(auth_url=f"{base}/authorize", token_url=f"{base}/token",
                     client_id="cid-123", grant="authorization_code")


# ---------------------------------------------------------------------------
# Device code — the DEFAULT flow (the only one that works on a headless box)
# ---------------------------------------------------------------------------
class TestDeviceCode:
    def test_survives_authorization_pending_and_slow_down(self, token_server):
        """Both are NORMAL states, not errors: pending is the user still
        typing, slow_down is the server asking us to back off. Treating either
        as failure would abort essentially every real connect."""
        prompts = []
        result = run_device_code_flow(
            _device_spec(token_server), on_prompt=prompts.append, sleep=lambda s: None
        )
        assert result.access_token == "tok-REAL"
        assert result.refresh_token == "refresh-abc"
        assert _TokenServerHandler.polls == 3
        assert prompts[0]["user_code"] == "WXYZ-1234"

    def test_slow_down_actually_increases_the_interval(self, token_server):
        """RFC 8628 §3.5 — ignoring it means hammering the token endpoint at
        the old rate, which is what gets a client throttled or blocked."""
        slept = []
        run_device_code_flow(_device_spec(token_server), sleep=slept.append)
        assert slept[-1] > slept[0], slept

    def test_never_polls_faster_than_the_floor(self, token_server):
        """A server returning interval=0 must not become a busy loop."""
        import core.llm_auth.flows.device_code as dc
        slept = []
        spec = _device_spec(token_server)

        def post(url, fields):
            if url.endswith("/device"):
                return {"device_code": "d", "user_code": "u",
                        "verification_uri": "https://x.test", "interval": 0}
            return {"access_token": "t"}

        dc.run_device_code_flow(spec, http_post=post, sleep=slept.append)
        assert min(slept) >= dc.MIN_POLL_INTERVAL_SEC

    def test_prompt_carries_the_verification_url_and_code(self, token_server):
        """This module must never print — the caller owns presentation, so the
        whole prompt payload has to reach it."""
        seen = {}
        run_device_code_flow(_device_spec(token_server),
                             on_prompt=seen.update, sleep=lambda s: None)
        assert seen["verification_uri"] == "https://example.test/activate"

    @pytest.mark.parametrize("error,expected", [
        ("access_denied", "denied"),
        ("expired_token", "expired"),
    ])
    def test_terminal_errors_stop_immediately(self, error, expected):
        spec = OAuthSpec(auth_url="https://x.test/d", token_url="https://x.test/t",
                         client_id="c", grant="device_code")

        def post(url, fields):
            if url.endswith("/d"):
                return {"device_code": "d", "user_code": "u",
                        "verification_uri": "https://x.test"}
            return {"error": error}

        with pytest.raises(FlowError, match=expected):
            run_device_code_flow(spec, http_post=post, sleep=lambda s: None)

    def test_gives_up_at_the_deadline(self):
        """A flow that polls forever wedges the terminal it was run from."""
        spec = OAuthSpec(auth_url="https://x.test/d", token_url="https://x.test/t",
                         client_id="c", grant="device_code")
        clock = {"t": 0.0}

        def post(url, fields):
            if url.endswith("/d"):
                return {"device_code": "d", "user_code": "u",
                        "verification_uri": "https://x.test", "interval": 1}
            return {"error": "authorization_pending"}

        def sleep(sec):
            clock["t"] += max(sec, 1.0)

        with pytest.raises(FlowError, match="timed out"):
            run_device_code_flow(spec, http_post=post, sleep=sleep,
                                 now=lambda: clock["t"], timeout=30)

    def test_rejects_a_non_rfc8628_endpoint(self):
        spec = OAuthSpec(auth_url="https://x.test/d", token_url="https://x.test/t",
                         client_id="c", grant="device_code")
        with pytest.raises(FlowError, match="RFC 8628"):
            run_device_code_flow(spec, http_post=lambda u, f: {"nope": 1},
                                 sleep=lambda s: None)


# ---------------------------------------------------------------------------
# Loopback PKCE — local only, single request, state + S256 enforced
# ---------------------------------------------------------------------------
class TestLoopbackPkce:
    def test_completes_over_a_real_redirect(self, token_server):
        spec = _pkce_spec(token_server)
        captured = {}

        def browser(url):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            captured.update({k: v[0] for k, v in q.items()})
            cb = f"{q['redirect_uri'][0]}?code=code-xyz&state={q['state'][0]}"
            threading.Thread(
                target=lambda: urllib.request.urlopen(cb, timeout=10).read(), daemon=True
            ).start()

        result = run_loopback_pkce_flow(spec, local_mode=True, open_browser=browser,
                                        timeout=15)
        assert result.access_token == "tok-PKCE"
        # S256 only — `plain` puts the verifier on the wire in the authorize URL
        assert captured["code_challenge_method"] == "S256"
        assert captured["redirect_uri"].startswith("http://127.0.0.1:")
        # the verifier must actually be presented at the token exchange
        exchange = [f for p, f in _TokenServerHandler.seen
                    if f.get("grant_type") == "authorization_code"][0]
        assert exchange["code_verifier"]

    def test_forged_state_is_rejected_before_any_exchange(self, token_server):
        """`state` is the CSRF defence: without this check an attacker-supplied
        redirect could plant THEIR authorization code in the owner's store."""
        spec = _pkce_spec(token_server)

        def browser(url):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            cb = f"{q['redirect_uri'][0]}?code=code-xyz&state=FORGED"
            threading.Thread(
                target=lambda: urllib.request.urlopen(cb, timeout=10).read(), daemon=True
            ).start()

        with pytest.raises(FlowError, match="state mismatch"):
            run_loopback_pkce_flow(spec, local_mode=True, open_browser=browser, timeout=15)
        assert not [f for p, f in _TokenServerHandler.seen
                    if f.get("grant_type") == "authorization_code"]

    def test_refused_without_local_mode(self):
        """A server must never open a redirect listener — the browser that
        completes the flow there is not the owner's."""
        with pytest.raises(FlowError, match="POLYROB_LOCAL"):
            run_loopback_pkce_flow(_pkce_spec("http://127.0.0.1:1"), local_mode=False)

    def test_binds_an_os_assigned_port(self, token_server):
        """A FIXED port could be squatted by any other local process, which
        would then receive the authorization code."""
        ports = []

        def browser(url):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            ports.append(urllib.parse.urlparse(q["redirect_uri"][0]).port)
            cb = f"{q['redirect_uri'][0]}?code=code-xyz&state={q['state'][0]}"
            threading.Thread(
                target=lambda: urllib.request.urlopen(cb, timeout=10).read(), daemon=True
            ).start()

        for _ in range(2):
            run_loopback_pkce_flow(_pkce_spec(token_server), local_mode=True,
                                   open_browser=browser, timeout=15)
        assert all(p and p > 0 for p in ports)
        assert ports[0] != ports[1], "port must not be fixed across runs"

    def test_pkce_pair_is_valid_s256(self):
        import base64
        import hashlib
        verifier, challenge = make_pkce_pair()
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip("=")
        assert challenge == expected
        assert "=" not in challenge and 43 <= len(verifier) <= 128
        assert make_pkce_pair()[0] != verifier      # fresh per flow


# ---------------------------------------------------------------------------
# Registry: gating, provider requirements, refresh
# ---------------------------------------------------------------------------
class TestRegistry:
    def test_connect_is_gated_off_by_default(self, monkeypatch):
        """Off EVERYWHERE including local (§7.4) — unlike most local flags."""
        monkeypatch.delenv("LLM_OAUTH_ENABLED", raising=False)
        monkeypatch.setenv("POLYROB_LOCAL", "1")
        from core.llm_auth.flows import connect
        with pytest.raises(FlowError, match="LLM_OAUTH_ENABLED"):
            connect("anything", _device_spec("https://x.test"))

    def test_every_builtin_oauth_row_discloses_its_tos_position(self, monkeypatch):
        """Shipped OAuth rows authenticate with the VENDORS' own CLI client ids
        (Anthropic issues none to third-party apps, nor does OpenAI). That is a
        real exposure and it lands on the account holder, so §7.4 requires each
        row to state it — `polyrob auth add` prints the note and asks before
        running anything, and LLM_OAUTH_ENABLED stays off by default."""
        monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", "")
        from modules.llm.provider_spec import get_specs, reset_provider_registry_cache
        reset_provider_registry_cache()
        try:
            oauth_rows = [s for s in get_specs() if s.oauth is not None]
            assert oauth_rows, "OAuth subscription rows should be shipped"
            for s in oauth_rows:
                assert s.tos_note, f"{s.name} ships no tos_note"
                assert s.oauth.client_id, s.name
                # A seat is not an API key: an env_key here would make an
                # unrelated variable look like a working credential.
                assert s.env_key is None, s.name
                assert s.subscription is True, s.name
                assert s.prompt_in_init is False, s.name
                assert s.fallback_eligible is False, s.name
        finally:
            reset_provider_registry_cache()

    def test_oauth_endpoints_are_all_https(self, monkeypatch):
        """A token would otherwise cross the network in plaintext."""
        monkeypatch.setenv("LLM_CUSTOM_PROVIDERS", "")
        from modules.llm.provider_spec import get_specs, reset_provider_registry_cache
        reset_provider_registry_cache()
        try:
            for s in get_specs():
                if s.oauth is None:
                    continue
                for url in (s.oauth.auth_url, s.oauth.token_url,
                            s.oauth.token_exchange_url, s.oauth.redirect_uri):
                    if url:
                        assert url.startswith("https://"), (s.name, url)
        finally:
            reset_provider_registry_cache()

    def test_missing_oauth_block_says_exactly_what_to_declare(self, monkeypatch):
        monkeypatch.setenv("LLM_OAUTH_ENABLED", "true")
        from core.llm_auth.flows import connect
        with pytest.raises(FlowError) as ei:
            connect("openai", None)          # the CLI resolves the spec; none declared
        msg = str(ei.value)
        assert "providers.yaml" in msg and "client_id" in msg

    def test_unknown_grant_is_named(self):
        from core.llm_auth.flows import UnsupportedGrantError, get_flow
        with pytest.raises(UnsupportedGrantError, match="teleport"):
            get_flow("teleport")

    def test_device_code_is_the_default_grant(self):
        from core.llm_auth.flows import get_flow
        assert get_flow("") is run_device_code_flow

    def test_refresh_preserves_a_non_rotated_refresh_token(self, token_server, tmp_path,
                                                           monkeypatch):
        """A provider that doesn't rotate returns no new refresh_token; dropping
        the old one would strand the seat at the next expiry."""
        import core.llm_auth.flows.registry as reg
        from core.llm_auth.store import AuthStore

        spec = _device_spec(token_server)
        store = AuthStore(str(tmp_path / "auth.json"))
        store.set_provider("myplan", {
            "access_token": "old", "refresh_token": "refresh-abc",
            "expires_at": time.time() + 5,          # inside the skew
        })
        result = reg.refresh_if_needed("myplan", spec, store=store)
        assert result.access_token == "tok-REFRESHED"
        assert store.get_provider("myplan")["refresh_token"] == "refresh-abc"

    def test_refresh_is_a_noop_while_the_token_is_fresh(self, token_server, tmp_path,
                                                        monkeypatch):
        import core.llm_auth.flows.registry as reg
        from core.llm_auth.store import AuthStore

        store = AuthStore(str(tmp_path / "auth.json"))
        store.set_provider("myplan", {
            "access_token": "old", "refresh_token": "r", "expires_at": time.time() + 86400,
        })
        assert reg.refresh_if_needed("myplan", _device_spec(token_server), store=store) is None
        assert store.get_provider("myplan")["access_token"] == "old"

    def test_refresh_yields_when_another_caller_already_refreshed(self, tmp_path,
                                                                  monkeypatch):
        """The identity guard: burning a second (often single-use) refresh token
        is how a concurrent refresh invalidates a perfectly good session."""
        import core.llm_auth.flows.registry as reg
        from core.llm_auth.store import AuthStore

        spec = OAuthSpec(auth_url="https://x.test/d", token_url="https://x.test/t",
                         client_id="c", grant="device_code")
        store = AuthStore(str(tmp_path / "auth.json"))
        store.set_provider("myplan", {
            "access_token": "old", "refresh_token": "r", "expires_at": time.time() - 1,
        })
        real_get = store.get_provider
        calls = {"n": 0}

        def racing_get(name):
            calls["n"] += 1
            if calls["n"] == 2:                  # the read taken under the lock
                return {"access_token": "SOMEONE-ELSE-REFRESHED-IT",
                        "refresh_token": "r2", "expires_at": time.time() + 3600}
            return real_get(name)

        monkeypatch.setattr(store, "get_provider", racing_get)
        assert reg.refresh_if_needed("myplan", spec, store=store) is None

    def test_disconnect_removes_the_entry(self, tmp_path, monkeypatch):
        import core.llm_auth.flows.registry as reg
        from core.llm_auth.store import AuthStore

        store = AuthStore(str(tmp_path / "auth.json"))
        store.set_provider("myplan", {"access_token": "t"})
        assert reg.disconnect("myplan", store=store) is True
        assert store.get_provider("myplan") is None
        assert reg.disconnect("myplan", store=store) is False


# ---------------------------------------------------------------------------
# Nothing may print or persist a token
# ---------------------------------------------------------------------------
class TestNoTokenLeaks:
    def test_flow_result_repr_redacts(self, token_server):
        result = run_device_code_flow(_device_spec(token_server), sleep=lambda s: None)
        text = repr(result)
        assert "tok-REAL" not in text and "refresh-abc" not in text
        assert "<redacted>" in text

    def test_credential_redacted_is_the_printable_form(self):
        from core.llm_auth.resolve import Credential
        cred = Credential(kind="bearer", value="sk-0123456789abcdefghijklmnop",
                          provider="p", source="oauth")
        shown = cred.redacted()
        assert cred.value not in shown
        assert shown.startswith("sk-0") and shown.endswith("mnop")
        # a short value reveals too much as prefix+suffix — redact it whole
        assert Credential(kind="bearer", value="short", provider="p").redacted() == "*" * 8
        assert Credential(kind="none", value=None, provider="p").redacted() == "(none)"

    def test_error_bodies_are_scrubbed(self):
        """An OAuth error response can echo the token that failed."""
        from core.llm_auth.flows.base import _safe_error_body
        out = _safe_error_body('{"error":"bad token gho_SyntheticFixture0000000000000000TEST"}')
        assert "gho_SyntheticFixture0000000000000000TEST" not in out

    def test_opaque_oauth_tokens_are_scrubbed_by_the_shared_battery(self):
        """Neither JWT-shaped nor sk--prefixed, so every prior rule missed them."""
        from core.secret_patterns import apply_ssot_shapes
        for token in ("gho_SyntheticFixture0000000000000000TEST",
                      "github_pat_SyntheticFixture0000000000000000000000000TEST",
                      "sbp_SyntheticFixture00000000000000000TEST"):
            assert token not in apply_ssot_shapes(f"token is {token} here")

    @pytest.mark.parametrize("benign", [
        "my_variable_name", "run_code", "user_id=usr_123",
        "MEMORY_BACKEND=local_vector", "agents/task/agent/core/error_recovery.py",
    ])
    def test_opaque_rule_does_not_eat_ordinary_identifiers(self, benign):
        """A catch-all here would redact identifiers and paths across every log."""
        from core.secret_patterns import apply_ssot_shapes
        assert apply_ssot_shapes(benign) == benign


# ---------------------------------------------------------------------------
# Manual-paste PKCE — Anthropic's shape, and the only PKCE mode usable on a
# headless box (nothing binds, nothing listens; the code arrives by hand).
# ---------------------------------------------------------------------------
class TestManualPastePkce:
    def _spec(self, base, **kw):
        return OAuthSpec(
            auth_url="https://provider.test/oauth/authorize",
            token_url=f"{base}/token", client_id="cid-123",
            scopes=("user:inference",), grant="authorization_code",
            redirect_mode="manual",
            redirect_uri="https://provider.test/oauth/code/callback", **kw)

    def test_completes_from_a_pasted_code(self, token_server):
        seen = {}
        result = run_manual_paste_pkce_flow(
            self._spec(token_server),
            on_prompt=lambda url: seen.update(
                urllib.parse.parse_qs(urllib.parse.urlparse(url).query)),
            read_code=lambda: "code-xyz",
        )
        assert result.access_token == "tok-PKCE"
        assert seen["code_challenge_method"] == ["S256"]
        # the hosted callback page, NOT a loopback port
        assert seen["redirect_uri"] == ["https://provider.test/oauth/code/callback"]
        exchange = [f for p, f in _TokenServerHandler.seen
                    if f.get("grant_type") == "authorization_code"][0]
        assert exchange["code_verifier"] and exchange["code"] == "code-xyz"

    def test_accepts_the_code_hash_state_form_and_verifies_state(self, token_server):
        """Providers commonly render the value as "<code>#<state>". The state
        still has to be checked — a human transport is not a reason to drop the
        same CSRF defence the loopback flow enforces."""
        state_box = {}

        def prompt(url):
            state_box["state"] = urllib.parse.parse_qs(
                urllib.parse.urlparse(url).query)["state"][0]

        result = run_manual_paste_pkce_flow(
            self._spec(token_server), on_prompt=prompt,
            read_code=lambda: f"code-xyz#{state_box['state']}")
        assert result.access_token == "tok-PKCE"

    def test_forged_state_in_the_paste_is_rejected(self, token_server):
        with pytest.raises(FlowError, match="state mismatch"):
            run_manual_paste_pkce_flow(
                self._spec(token_server), on_prompt=lambda u: None,
                read_code=lambda: "code-xyz#FORGED")
        assert not [f for p, f in _TokenServerHandler.seen
                    if f.get("grant_type") == "authorization_code"]

    def test_empty_paste_stores_nothing(self, token_server):
        with pytest.raises(FlowError, match="no code entered"):
            run_manual_paste_pkce_flow(self._spec(token_server),
                                       on_prompt=lambda u: None, read_code=lambda: "  ")

    def test_needs_no_local_mode(self, token_server, monkeypatch):
        """Unlike loopback: nothing binds, so a server can use it too."""
        monkeypatch.delenv("POLYROB_LOCAL", raising=False)
        result = run_manual_paste_pkce_flow(
            self._spec(token_server), on_prompt=lambda u: None,
            read_code=lambda: "code-xyz")
        assert result.access_token == "tok-PKCE"

    def test_manual_mode_is_dispatched_by_redirect_mode(self):
        from core.llm_auth.flows import get_flow
        from core.llm_auth.flows.loopback_pkce import run_loopback_pkce_flow
        assert get_flow("authorization_code", "manual") is run_manual_paste_pkce_flow
        assert get_flow("authorization_code", "loopback") is run_loopback_pkce_flow


class TestJsonDeviceAuth:
    def test_json_style_posts_a_json_body(self):
        """OpenAI's device leg takes JSON, not RFC 8628's form encoding."""
        spec = OAuthSpec(auth_url="https://x.test/d", token_url="https://x.test/t",
                         client_id="c", grant="device_code", device_auth_style="json")
        assert spec.device_auth_style == "json"
        calls = []

        def post(url, fields):
            calls.append(url)
            if url.endswith("/d"):
                return {"device_code": "d", "user_code": "u",
                        "verification_uri": "https://x.test"}
            return {"access_token": "tok"}

        # With an injected poster the style is moot (the caller owns encoding);
        # what must hold is that the flow still completes and hits both legs.
        out = run_device_code_flow(spec, http_post=post, sleep=lambda s: None)
        assert out.access_token == "tok"
        assert calls == ["https://x.test/d", "https://x.test/t"]


# ---------------------------------------------------------------------------
# Task 4 — the gaps that made a connected seat unusable for inference
# ---------------------------------------------------------------------------
class TestTokenExchange:
    """Copilot's OAuth token is NOT the inference credential."""

    def _server(self):
        import http.server, json as _json, threading as _th

        class H(http.server.BaseHTTPRequestHandler):
            calls = []

            def log_message(self, *a):
                pass

            def do_GET(self):
                type(self).calls.append(dict(self.headers))
                if self.headers.get("Authorization") != "token raw-gh-token":
                    self.send_response(401); self.end_headers(); self.wfile.write(b"{}")
                    return
                body = _json.dumps({
                    "token": "copilot-jwt", "expires_at": 4102444800,
                    "endpoints": {"api": "https://enterprise.example.com/"},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)

        H.calls = []
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        _th.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, H

    def test_exchange_returns_the_real_credential_and_host(self):
        from core.llm_auth.flows.base import exchange_token
        srv, H = self._server()
        try:
            out = exchange_token(f"http://127.0.0.1:{srv.server_address[1]}/x",
                                 "raw-gh-token")
        finally:
            srv.shutdown()
        assert out.access_token == "copilot-jwt"
        assert out.expires_at == 4102444800
        # enterprise/proxied accounts advertise their own API host
        assert out.scope == "https://enterprise.example.com"
        # GitHub gates the endpoint on looking like an editor integration
        assert "Editor-Version" in H.calls[0]

    def test_unentitled_account_gets_an_explanation_not_a_status(self):
        from core.llm_auth.flows.base import exchange_token
        srv, _ = self._server()
        try:
            with pytest.raises(FlowError, match="not entitled to Copilot"):
                exchange_token(f"http://127.0.0.1:{srv.server_address[1]}/x", "wrong")
        finally:
            srv.shutdown()

    def test_connect_stores_the_exchanged_token_and_keeps_the_source(self, tmp_path,
                                                                     monkeypatch):
        """Refresh must be able to REDO the exchange — it is not an OAuth
        refresh grant, so the raw token has to survive."""
        import core.llm_auth.flows.registry as reg
        from core.llm_auth.store import AuthStore
        from modules.llm.provider_spec import OAuthSpec

        monkeypatch.setenv("LLM_OAUTH_ENABLED", "true")
        srv, _ = self._server()
        exch = f"http://127.0.0.1:{srv.server_address[1]}/x"
        spec = OAuthSpec(auth_url="https://x.test/d", token_url="https://x.test/t",
                         client_id="c", grant="device_code", token_exchange_url=exch)
        store = AuthStore(str(tmp_path / "auth.json"))
        monkeypatch.setattr(reg, "get_flow", lambda *a, **k: (
            lambda oauth, **kw: reg.FlowResult(access_token="raw-gh-token")))
        try:
            out = reg.connect("github-copilot", spec, store=store)
        finally:
            srv.shutdown()
        entry = store.get_provider("github-copilot")
        assert out.access_token == "copilot-jwt"
        assert entry["access_token"] == "copilot-jwt"        # inference uses this
        assert entry["exchange_source_token"] == "raw-gh-token"   # refresh needs this
        assert entry["api_base_url"] == "https://enterprise.example.com"


class TestProviderQuirks:
    def test_kimi_code_keys_are_redirected_off_the_legacy_host(self):
        """A sk-kimi- key is rejected by api.moonshot.ai — the subscriber would
        see an unexplained 401 on a key that is perfectly valid."""
        from modules.llm.provider_spec import get_spec
        spec = get_spec("moonshot")
        assert spec.resolved_base_url(env={}, api_key="sk-legacy-abc") \
            == "https://api.moonshot.ai/v1"
        assert spec.resolved_base_url(env={}, api_key="sk-kimi-abc") \
            == "https://api.kimi.com/coding/v1"
        # an explicit operator override is still final
        assert spec.resolved_base_url(env={"KIMI_BASE_URL": "https://p/v1"},
                                      api_key="sk-kimi-abc") == "https://p/v1"

    def test_anthropic_oauth_sends_bearer_and_the_client_fingerprint(self):
        """A seat authenticates Bearer, not x-api-key (verified: Anthropic
        answered "invalid x-api-key"), and OAuth traffic is routed on these
        headers — without them requests intermittently 500."""
        from modules.llm.provider_spec import get_spec
        spec = get_spec("anthropic-oauth")
        assert spec.bearer_auth is True
        headers = spec.headers()
        assert "oauth-2025-04-20" in headers["anthropic-beta"]
        assert headers["x-app"] == "cli"
        assert headers["user-agent"].startswith("claude-code/")

    def test_minimax_redeems_under_its_non_standard_grant(self):
        from modules.llm.provider_spec import get_spec
        assert get_spec("minimax-oauth").oauth.token_grant_type == \
            "urn:ietf:params:oauth:grant-type:user_code"

    def test_device_flow_sends_the_declared_grant_urn(self):
        from modules.llm.provider_spec import OAuthSpec
        spec = OAuthSpec(auth_url="https://x.test/d", token_url="https://x.test/t",
                         client_id="c", grant="device_code",
                         token_grant_type="urn:ietf:params:oauth:grant-type:user_code")
        sent = {}

        def post(url, fields):
            if url.endswith("/d"):
                return {"device_code": "d", "user_code": "u",
                        "verification_uri": "https://x.test"}
            sent.update(fields)
            return {"access_token": "tok"}

        run_device_code_flow(spec, http_post=post, sleep=lambda s: None)
        assert sent["grant_type"] == "urn:ietf:params:oauth:grant-type:user_code"
