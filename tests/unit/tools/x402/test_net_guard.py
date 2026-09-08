"""x402 network guard — the paying verbs get the hardening the prober already had.

H1b/N-D/N-E/N-F (audit 2026-08-22): `x402_quote` and `x402_fetch` sent an
agent-supplied URL through a bare httpx client — no SSRF validator, no DNS
pinning, no redirect control, no size cap, no total timeout — while the
read-only `x402_probe` had all five. The hardening was on the verb that cannot
pay and absent from the one that returns the response body.
"""
import pytest

from tools.x402 import net_guard


class _Validator:
    """Stands in for tools.mcp.security.MCPURLValidator."""

    def __init__(self, ok=True, err=None, ip="93.184.216.34"):
        self._ok, self._err, self._ip = ok, err, ip
        self.calls = []

    def validate_and_resolve(self, url):
        self.calls.append(url)
        return (self._ok, self._err, self._ip if self._ok else None)


@pytest.mark.asyncio
async def test_metadata_url_is_refused():
    v = _Validator(ok=False, err="blocked network 169.254.0.0/16")
    refusal, pinned = await net_guard.validate_x402_url(
        "http://169.254.169.254/latest/meta-data/", validator=v)
    assert refusal and "blocked" in refusal.lower()
    assert pinned is None


@pytest.mark.asyncio
async def test_public_url_is_allowed_and_pinned():
    v = _Validator(ok=True, ip="93.184.216.34")
    refusal, pinned = await net_guard.validate_x402_url(
        "https://api.example.com/paid", validator=v)
    assert refusal is None
    assert pinned == "93.184.216.34"


@pytest.mark.asyncio
async def test_a_raising_validator_refuses_fail_closed():
    class _Boom:
        def validate_and_resolve(self, url):
            raise RuntimeError("resolver exploded")

    refusal, pinned = await net_guard.validate_x402_url("https://x/", validator=_Boom())
    assert refusal and "validator error" in refusal
    assert pinned is None


@pytest.mark.asyncio
async def test_own_published_sandbox_port_is_the_one_loopback_exception(monkeypatch):
    """Parity with discovery._validate: the agent must still be able to pay/probe
    its OWN published dev-container port — never RFC1918, never metadata."""
    monkeypatch.setattr(
        "tools.shell.loopback_allow.is_loopback_allowed", lambda u: True)
    refusal, pinned = await net_guard.validate_x402_url(
        "http://127.0.0.1:8931/paid", validator=_Validator(ok=False))
    assert refusal is None
    assert pinned == "127.0.0.1"


@pytest.mark.asyncio
async def test_validation_runs_off_the_event_loop_thread():
    """validate_and_resolve does a blocking getaddrinfo. It MUST run in an executor
    thread, or a sinkholed DNS freezes the event loop for every other session.

    Asserting the validator ran on a DIFFERENT thread than the loop is the only
    check that actually proves the offload — asserting "the loop still ticked"
    passes whether or not run_in_executor was used."""
    import threading

    loop_thread = threading.get_ident()
    seen = {}

    class _ThreadRecordingValidator:
        def validate_and_resolve(self, url):
            seen["thread"] = threading.get_ident()
            return (True, None, "1.2.3.4")

    refusal, pinned = await net_guard.validate_x402_url(
        "https://x/", validator=_ThreadRecordingValidator())
    assert refusal is None and pinned == "1.2.3.4"
    assert seen["thread"] != loop_thread, (
        "validate_and_resolve ran ON the event loop thread — a blocking "
        "getaddrinfo there freezes every other session")


def test_pinned_transport_preserves_host_header_and_sni():
    """Rewriting the URL host to the IP without preserving Host + SNI breaks TLS
    and virtual hosting. Both must survive."""
    import httpx

    sent = {}

    class _Inner(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            sent["url"] = str(request.url)
            sent["host"] = request.headers.get("Host")
            sent["sni"] = request.extensions.get("sni_hostname")
            return httpx.Response(200, content=b"ok")

    t = net_guard.PinnedAsyncTransport("api.example.com", "93.184.216.34", inner=_Inner())
    req = httpx.Request("GET", "https://api.example.com/paid")
    import asyncio
    resp = asyncio.run(t.handle_async_request(req))
    assert resp.status_code == 200
    assert sent["url"].startswith("https://93.184.216.34/")
    assert sent["host"] == "api.example.com"
    assert sent["sni"] == "api.example.com"


def test_bounds_are_defined_and_match_the_prober():
    from tools.x402.discovery import MAX_PROBE_BYTES
    assert net_guard.MAX_X402_BODY_BYTES == MAX_PROBE_BYTES
    assert net_guard.X402_HTTP_TIMEOUT_SEC > 0
