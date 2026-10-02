"""``get_json`` rides the SAME pooled client as the rest of the providers.

The 2026-09-24 pooling fix moved `dexscreener`, `goplus` and `alchemy_index`
onto `_http.client()` and left `get_json` on a per-call
`urllib.request.urlopen`, which pays the box's broken-IPv6 handshake on EVERY
call (~6.2 s measured; see `test_provider_connection_reuse.py` for the numbers).
Three providers still went through it — `geckoterminal` (pool discovery),
`jupiter` (Solana quotes) and `relay_bridge.status` (has my bridge arrived?) —
so the reads those verbs make stayed slow: `token_resolve` 14.7 s,
`token_info` 16.6 s, `swap_quote` 13.4 s, `ohlcv` 12.8 s.

⚠️ The subtle part is NOT the speed, it is the exception contract. `urlopen`
RAISES `HTTPError` on 4xx/5xx and that error carries `.code`; `httpx` does
neither — it returns a response object and its own error exposes
`.response.status_code`. `geckoterminal._is_rate_limited` reads `.code`, so a
naive port would have silently turned "429, back off and retry once" into "this
chain has no pools", which is a WRONG ANSWER rather than a slow one.
"""
import pytest

from tools.defi.providers import _http


class _Response:
    def __init__(self, status_code=200, payload=None, reason_phrase="OK"):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.reason_phrase = reason_phrase

    def json(self):
        return self._payload


class _RecordingClient:
    """Stands in for the pooled client and remembers how it was called."""

    def __init__(self, response=None):
        self.calls = []
        self._response = response or _Response()

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response


@pytest.fixture()
def pooled(monkeypatch):
    client = _RecordingClient()
    monkeypatch.setattr(_http, "client", lambda: client)
    return client


# --- it uses the pool at all ----------------------------------------------- #

def test_get_json_goes_through_the_pooled_client(pooled):
    assert _http.get_json("https://example.test/x", timeout=5.0) == {"ok": True}
    assert len(pooled.calls) == 1
    assert pooled.calls[0][0] == "https://example.test/x"


def test_get_json_does_not_open_its_own_connection(monkeypatch, pooled):
    """A per-call `urlopen` is exactly the ~6 s handshake this fix removes."""
    def _boom(*a, **kw):  # pragma: no cover - the assertion is that it is unused
        raise AssertionError("get_json opened its own connection")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    assert _http.get_json("https://example.test/x", timeout=5.0) == {"ok": True}


def test_the_module_no_longer_reaches_for_urlopen():
    """Source ratchet: the next edit must not quietly reintroduce it.

    Walked as an AST rather than grepped, because the module DOCSTRINGS name
    `urlopen` when explaining why it is gone — a text search would match its own
    explanation, which is how three ratchets in this tree have failed before.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(_http))
    referenced = {node.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Attribute)}
    referenced |= {node.id for node in ast.walk(tree)
                   if isinstance(node, ast.Name)}
    assert "urlopen" not in referenced


# --- the exception contract, which is the part that can go wrong silently --- #

def test_a_non_2xx_raises_rather_than_returning_the_body(monkeypatch):
    """`urlopen` raised on 4xx/5xx. httpx returns a response, so a port that
    forgets `raise_for_status` hands an ERROR PAGE to a parser as if it were
    data — the failure mode is a confidently wrong answer."""
    monkeypatch.setattr(_http, "client",
                        lambda: _RecordingClient(
                            _Response(503, {"error": "upstream"}, "Service Unavailable")))
    with pytest.raises(Exception) as exc:
        _http.get_json("https://example.test/x", timeout=5.0)
    assert "503" in str(exc.value)


def test_the_raised_error_carries_code_like_HTTPError_did(monkeypatch):
    """`geckoterminal._is_rate_limited` reads `.code`. Losing it downgrades a
    retryable 429 into a permanent empty result."""
    monkeypatch.setattr(_http, "client",
                        lambda: _RecordingClient(
                            _Response(429, {}, "Too Many Requests")))
    with pytest.raises(Exception) as exc:
        _http.get_json("https://example.test/x", timeout=5.0)
    assert getattr(exc.value, "code", None) == 429


def test_geckoterminal_still_recognises_a_rate_limit(monkeypatch):
    """The end-to-end version of the test above, against the real predicate."""
    from tools.defi.providers import geckoterminal

    monkeypatch.setattr(_http, "client",
                        lambda: _RecordingClient(
                            _Response(429, {}, "Too Many Requests")))
    try:
        _http.get_json("https://example.test/x", timeout=5.0)
    except Exception as exc:
        assert geckoterminal._is_rate_limited(exc) is True
    else:  # pragma: no cover
        pytest.fail("a 429 must raise")


def test_a_200_is_not_treated_as_an_error(pooled):
    assert _http.get_json("https://example.test/x", timeout=5.0) == {"ok": True}


# --- the per-call knobs the callers actually pass --------------------------- #

def test_the_per_call_timeout_reaches_the_request(pooled):
    """Callers pass their own budget (`relay_bridge` a bridge-status budget,
    `geckoterminal` TIMEOUT_SEC). The pooled client has ONE default timeout, so
    a port that drops the argument silently re-times every caller."""
    _http.get_json("https://example.test/x", timeout=3.5)
    assert pooled.calls[0][1].get("timeout") == 3.5


def test_the_user_agent_override_reaches_the_request(pooled):
    """`relay_bridge` identifies itself as polyrob-bridge/1.0."""
    _http.get_json("https://example.test/x", timeout=5.0,
                   user_agent="polyrob-bridge/1.0")
    headers = pooled.calls[0][1].get("headers") or {}
    assert headers.get("user-agent") == "polyrob-bridge/1.0"


def test_the_default_user_agent_is_the_project_one(pooled):
    _http.get_json("https://example.test/x", timeout=5.0)
    headers = pooled.calls[0][1].get("headers") or {}
    assert headers.get("user-agent") == _http.USER_AGENT
