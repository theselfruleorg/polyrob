"""093 F1: the http_request verb (one SSRF path, no ambient credentials, D2 posture)."""
from types import SimpleNamespace

import pytest

import tools  # noqa: F401  (registers the gates and the explain probe)
from tools.code_exec import backend_probe


@pytest.fixture(autouse=True)
def _fresh_probe(monkeypatch):
    for k in ("CODE_EXEC_BACKEND", "DOCKER_HOST", "SHELL_BACKEND", "CODE_EXEC_SSH_HOST",
              "CODE_EXEC_DOCKER_BINARY"):
        monkeypatch.delenv(k, raising=False)
    backend_probe.reset_probe_cache()
    yield
    backend_probe.reset_probe_cache()


class _Container:
    def __init__(self, services):
        self._s = services

    def has_service(self, name):
        return name in self._s

    def get_service(self, name):
        return self._s[name]


# -- F1: http_request ---------------------------------------------------------------

def test_http_request_is_classified_as_a_network_write():
    from core.tool_capabilities import TOOL_CAPABILITIES, TOOL_PERMISSIONS, row_field
    from core.verb_policy import policy_for
    row = TOOL_CAPABILITIES["http_request"]
    assert {"high_impact", "writes_network"} <= set(row)
    # 093 review: the tool refuses a sub-agent, so a leaf's catalog never offers it.
    assert "delegate_blocked" in row
    assert row_field("http_request", "untrusted_output") is True
    assert "network.write" in TOOL_PERMISSIONS["http_request"]
    assert policy_for("http_request").correspondent_blocked is True


class _Resp:
    def __init__(self, status, headers, body=b""):
        self.status, self.headers, self._body = status, headers, body
        self.content = self

    async def iter_chunked(self, n):
        if self._body:
            yield self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, script, log):
        self._script, self._log = script, log

    def _next(self, method, url, headers, json=None):
        self._log.append((method, url, headers, json))
        return _Resp(*self._script.pop(0))

    def get(self, url, allow_redirects, timeout, headers=None):
        return self._next("GET", url, headers)

    def post(self, url, allow_redirects, timeout, headers=None, json=None):
        return self._next("POST", url, headers, json)

    def request(self, method, url, allow_redirects, timeout, headers=None, json=None):
        return self._next(method, url, headers, json)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _factory(script, log):
    return lambda pinned_ip: _Session(script, log)


@pytest.mark.asyncio
async def test_safe_fetch_sends_put_with_a_json_body():
    from tools.web_fetch.fetcher import safe_fetch
    log = []
    res = await safe_fetch("https://api.example.com/x", method="PUT", json_body={"a": 1},
                           validate=False,
                           session_factory=_factory([(200, {"Content-Type": "application/json"},
                                                      b'{"ok":true}')], log))
    assert res.status == 200 and log[0][0] == "PUT" and log[0][3] == {"a": 1}


@pytest.mark.asyncio
async def test_safe_fetch_refuses_a_redirect_to_another_host_when_asked():
    from tools.web_fetch.fetcher import WebFetchError, safe_fetch
    script = [(302, {"Location": "https://other.example.org/"}, b"")]
    with pytest.raises(WebFetchError, match="another origin"):
        await safe_fetch("https://api.example.com/x", validate=False, same_host_redirects=True,
                         session_factory=_factory(script, []))


def _ctx(session_id="s1", user_id="u1", role="orchestrator", **kw):
    return SimpleNamespace(session_id=session_id, user_id=user_id, metadata={},
                           role=role, **kw)


@pytest.fixture(autouse=True)
def _owner_tenant(monkeypatch):
    import core.security.owner_turn as ot
    monkeypatch.setattr(ot, "is_owner_tenant", lambda ctx: getattr(ctx, "user_id", "") == "u1")


@pytest.fixture
def http_tool(monkeypatch):
    from tools.http_request import tool as mod
    mod._APPROVED.clear()
    sent = []

    async def fake_fetch(url, **kw):
        sent.append((url, kw))
        from tools.web_fetch.fetcher import FetchResult
        return FetchResult(final_url=url, status=201, content_type="application/json",
                           body=b'{"enrolled":true}')

    monkeypatch.setattr("tools.web_fetch.fetcher.safe_fetch", fake_fetch)
    yield mod, object.__new__(mod.HttpRequestTool), sent
    mod._APPROVED.clear()


@pytest.mark.asyncio
async def test_owner_turn_sends_without_an_ask(http_tool, monkeypatch):
    mod, tool, sent = http_tool
    monkeypatch.setattr(mod, "_autonomous", lambda ctx: False)

    async def no_ask(*a, **k):
        raise AssertionError("an owner turn never asks")

    monkeypatch.setattr(mod, "_ask_owner_for_domain", no_ask)
    params = mod.HttpRequestParams(url="https://api.example.com/enroll",
                                   body_json={"wallet": "0xabc"},
                                   headers_json={"Authorization": "Bearer t"})
    res = await tool.http_request(params, execution_context=_ctx())
    assert res.error is None and "HTTP 201" in res.extracted_content
    url, kw = sent[0]
    assert kw["method"] == "POST" and kw["json_body"] == {"wallet": "0xabc"}
    assert kw["extra_headers"] == {"Authorization": "Bearer t"}
    assert kw["same_host_redirects"] is True and kw["max_bytes"] == mod.MAX_RESPONSE_BYTES


@pytest.mark.asyncio
async def test_bad_shapes_are_refused_before_any_send(http_tool, monkeypatch):
    mod, tool, sent = http_tool
    monkeypatch.setattr(mod, "_autonomous", lambda ctx: False)
    for p in (dict(url="https://a.example.com/", headers_json={"Host": "evil"}),
              dict(url="https://a.example.com/", headers_json={"X": "a\r\nb"}),
              dict(url="https://a.example.com/", method="GET", body_json={"a": 1}),
              dict(url="https://a.example.com/", body_json='"a string"'),
              dict(url="ftp://a.example.com/")):
        res = await tool.http_request(mod.HttpRequestParams(**p), execution_context=_ctx())
        assert res.error, p
    assert sent == []


@pytest.mark.asyncio
async def test_autonomous_run_asks_once_per_domain_and_remembers(http_tool, monkeypatch):
    mod, tool, sent = http_tool
    monkeypatch.setattr(mod, "_autonomous", lambda ctx: True)
    monkeypatch.setattr(mod, "domain_decision", lambda uid, host: (
        "approved" if (uid, host) in mod._APPROVED else None))
    asks = []

    async def ask(uid, host, method, url, ctx):
        asks.append(host)
        return True

    monkeypatch.setattr(mod, "_ask_owner_for_domain", ask)
    for path in ("a", "b"):
        res = await tool.http_request(
            mod.HttpRequestParams(url=f"https://api.example.com/{path}"),
            execution_context=_ctx())
        assert res.error is None
    assert asks == ["api.example.com"] and len(sent) == 2


@pytest.mark.asyncio
async def test_autonomous_run_without_approval_sends_nothing(http_tool, monkeypatch):
    mod, tool, sent = http_tool
    monkeypatch.setattr(mod, "_autonomous", lambda ctx: True)
    monkeypatch.setattr(mod, "domain_decision", lambda uid, host: None)

    async def ask(*a):
        return False

    monkeypatch.setattr(mod, "_ask_owner_for_domain", ask)
    res = await tool.http_request(mod.HttpRequestParams(url="https://api.example.com/a"),
                                  execution_context=_ctx())
    assert "not approved requests to api.example.com" in res.error and sent == []


@pytest.mark.asyncio
async def test_a_declined_domain_is_not_asked_again(http_tool, monkeypatch):
    mod, tool, sent = http_tool
    monkeypatch.setattr(mod, "_autonomous", lambda ctx: True)
    monkeypatch.setattr(mod, "domain_decision", lambda uid, host: "rejected")

    async def ask(*a):
        raise AssertionError("a declined domain is not asked again")

    monkeypatch.setattr(mod, "_ask_owner_for_domain", ask)
    res = await tool.http_request(mod.HttpRequestParams(url="https://api.example.com/a"),
                                  execution_context=_ctx())
    assert "declined" in res.error and sent == []


def test_domain_decision_reads_the_owner_queue_record(monkeypatch):
    from tools.http_request import tool as mod
    from tools.controller.approval_queue import compute_request_hash
    mod._APPROVED.clear()
    h = compute_request_hash("http_request", {"domain": "api.example.com"}, "u1")

    class _Board:
        def asks(self, *, user_id, status):
            if status == "fulfilled":
                return [SimpleNamespace(payload={"ask_kind": "tool_approval",
                                                 "request_hash": h, "decision": "approved"})]
            return []

    monkeypatch.setattr(mod, "_ask_board", lambda: _Board())
    assert mod.domain_decision("u1", "api.example.com") == "approved"
    assert mod.domain_decision("u1", "other.example.com") is None
    mod._APPROVED.clear()



@pytest.mark.asyncio
@pytest.mark.parametrize("ctx", [
    dict(role="leaf"),                                   # a leaf of an owner chat
    dict(is_sub_agent=True),                             # a delegated sub-agent
    dict(user_id="stranger"),                            # a non-owner tenant
])
async def test_a_leaf_sub_agent_or_stranger_never_sends(http_tool, ctx):
    mod, tool, sent = http_tool
    res = await tool.http_request(mod.HttpRequestParams(url="https://evil.example/hook",
                                                        body_json='{"a":1}'),
                                  execution_context=_ctx(**ctx))
    assert res.error and "denied" in res.error and not sent


@pytest.mark.asyncio
async def test_a_same_host_redirect_may_not_downgrade_or_change_port():
    from tools.web_fetch.fetcher import _origin_key
    assert _origin_key("https://a.example/x") == _origin_key("https://A.example:443/y")
    assert _origin_key("https://a.example/x") != _origin_key("http://a.example/x")
    assert _origin_key("https://a.example/x") != _origin_key("https://a.example:8443/x")


def test_a_decline_expires(monkeypatch):
    import time
    from tools.http_request import tool as mod
    from tools.controller.approval_queue import compute_request_hash
    want = compute_request_hash(mod.ACTION, {"domain": "old.example"}, "u1")

    class Board:
        def asks(self, user_id, status):
            if status != "rejected":
                return []
            return [SimpleNamespace(payload={"ask_kind": "tool_approval", "request_hash": want,
                                             "decision": "rejected"},
                                    created_at=time.time() - mod.DECLINE_TTL_SEC - 1)]
    monkeypatch.setattr(mod, "_ask_board", lambda: Board())
    assert mod.domain_decision("u1", "old.example") is None
