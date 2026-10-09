"""Wave 4 Task 2 — Signal parse / surface / harness (mocked transports)."""
import asyncio
import types

import pytest

from core.surfaces.envelopes import OutboundMessage
from surfaces.signal.surface import (SignalSurface, parse_envelope,
                                     target_from_session_key)

ACCOUNT = "+15550000000"
SENDER_UUID = "d71910b1-9c34-4b90-9cd8-88428d03e1b2"


def _envelope(**kw):
    e = {"source": "+15551112222", "sourceNumber": "+15551112222",
         "sourceUuid": SENDER_UUID, "sourceName": "alice", "timestamp": 1720000000000,
         "dataMessage": {"message": "hello"}}
    e.update(kw)
    return e


def test_parse_dm():
    inbound = parse_envelope(_envelope(), ACCOUNT)
    assert inbound.text == "hello"
    assert inbound.identity.source.chat_type == "dm"
    assert inbound.identity.source.chat_id == SENDER_UUID
    assert inbound.identity.user_id == "u_signal_" + SENDER_UUID
    assert inbound.mentions_bot is None
    assert inbound.idempotency_key == SENDER_UUID + ":1720000000000"


def test_parse_group():
    inbound = parse_envelope(_envelope(
        dataMessage={"message": "hi", "groupInfo": {"groupId": "g=="}}),
        ACCOUNT)
    assert inbound.identity.source.chat_type == "group"
    assert inbound.identity.source.chat_id == "group.g=="


def test_parse_skips_own_receipts_and_empty():
    assert parse_envelope(_envelope(source=ACCOUNT,
                                    sourceNumber=ACCOUNT), ACCOUNT) is None
    assert parse_envelope({"source": "+1", "receiptMessage": {}},
                          ACCOUNT) is None
    assert parse_envelope(_envelope(dataMessage={"message": ""}),
                          ACCOUNT) is None


class _FakeClient:
    account = ACCOUNT

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail
        self.daemon_url = "http://127.0.0.1:8080"

    async def send(self, recipient, text):
        if self.fail:
            raise RuntimeError("boom")
        self.sent.append((recipient, text))

    async def close(self):
        return None


def test_surface_send_splits_and_targets(monkeypatch):
    monkeypatch.setenv("SIGNAL_SEND_MIN_INTERVAL_SEC", "0")
    client = _FakeClient()
    surface = SignalSurface(client)
    res = asyncio.run(surface.send(OutboundMessage(
        session_key="agent:main:signal:dm:+15551112222:u_x", text="z" * 4500)))
    assert res.success and len(client.sent) == 3
    assert client.sent[0][0] == "+15551112222"
    assert target_from_session_key(
        "agent:main:signal:group:group.abc") == "group.abc"


def test_surface_send_fail_open(monkeypatch):
    monkeypatch.setenv("SIGNAL_SEND_MIN_INTERVAL_SEC", "0")
    res = asyncio.run(SignalSurface(_FakeClient(fail=True)).send(
        OutboundMessage(session_key="agent:main:signal:dm:+1:u_x", text="x")))
    assert res.success is False


@pytest.mark.asyncio
async def test_harness_dedup_and_delivery(tmp_path, monkeypatch):
    from surfaces.signal.harness import build_signal_harness

    class _Container:
        def __init__(self):
            self._svc = {}
            self.config = types.SimpleNamespace(data_dir=str(tmp_path))

        def get_service(self, name):
            return self._svc.get(name)

        def register_service(self, name, svc):
            self._svc[name] = svc

    harness = build_signal_harness(_Container(), task_agent=None,
                                   daemon_url="http://127.0.0.1:9",
                                   account=ACCOUNT, data_dir=str(tmp_path))
    sent = []

    async def fake_send(recipient, text):
        sent.append((recipient, text))

    monkeypatch.setattr(harness._client, "send", fake_send)

    async def fake_act(task_agent, result, deliver=None, **kw):
        return "ack!"

    monkeypatch.setattr("surfaces.telegram.harness.act_on_inbound", fake_act)

    await harness.handle_envelope(_envelope())
    assert sent == [(SENDER_UUID, "ack!")]
    await harness.handle_envelope(_envelope())  # same timestamp -> dedup
    assert len(sent) == 1


# --- finalization hardening: envelope unwrap, account param, uuid fallback ---


def test_extract_envelope_shapes():
    from surfaces.signal.client import extract_envelope
    env = {"sourceNumber": "+1", "dataMessage": {"message": "hi"}}
    assert extract_envelope({"envelope": env}) is env
    # stdio JSON-RPC notification wrapper (some daemon builds)
    assert extract_envelope({"jsonrpc": "2.0", "method": "receive",
                             "params": {"envelope": env}}) is env
    # already-unwrapped envelope passes through
    assert extract_envelope(env) is env


class _FakeRpcResp:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, content_type=None):
        return self._payload


class _FakeRpcSession:
    def __init__(self):
        self.posts = []

    def post(self, url, json=None):
        self.posts.append((url, json))
        return _FakeRpcResp({"jsonrpc": "2.0", "id": 1,
                             "result": {"timestamp": 1}})


def _client_with_session(account):
    from surfaces.signal.client import SignalClient
    client = SignalClient(daemon_url="http://127.0.0.1:8080", account=account)
    fake = _FakeRpcSession()

    async def _http():
        return fake

    client._http = _http
    return client, fake


def test_send_omits_empty_account_param():
    client, fake = _client_with_session(account="")
    asyncio.run(client.send("+15551234", "hi"))
    _, body = fake.posts[0]
    assert "account" not in body["params"]
    assert body["params"]["recipient"] == ["+15551234"]


def test_send_includes_configured_account():
    client, fake = _client_with_session(account="+19995550000")
    asyncio.run(client.send("group.g==", "hi"))
    _, body = fake.posts[0]
    assert body["params"]["account"] == "+19995550000"
    assert body["params"]["groupId"] == "g=="


def test_parse_envelope_source_uuid_fallback():
    from surfaces.signal.surface import parse_envelope
    env = {"sourceUuid": SENDER_UUID, "timestamp": 5,
           "dataMessage": {"message": "hello"}}
    inbound = parse_envelope(env, account="+19995550000")
    assert inbound is not None
    assert inbound.identity.raw_user_id == SENDER_UUID
    assert inbound.identity.user_id == "u_signal_" + SENDER_UUID


def test_number_visibility_cannot_change_tenant_or_dedup():
    visible = parse_envelope(_envelope(), ACCOUNT)
    hidden = parse_envelope(_envelope(source=None, sourceNumber=None), ACCOUNT)
    changed = parse_envelope(_envelope(source="+15559999999", sourceNumber="+15559999999"), ACCOUNT)
    assert visible.identity == hidden.identity == changed.identity
    assert visible.idempotency_key == hidden.idempotency_key == changed.idempotency_key


@pytest.mark.parametrize("sender_uuid", [None, "", "not-a-uuid"])
def test_phone_only_identity_is_refused(sender_uuid):
    assert parse_envelope(_envelope(sourceUuid=sender_uuid), ACCOUNT) is None


import pytest as _pytest


@_pytest.mark.parametrize("url,ok", [
    ("http://127.0.0.1:8080", True),
    ("http://localhost:8080", True),
    ("http://[::1]:8080", True),
    ("https://signal.example.org", True),
    ("http://10.0.0.5:8080", False),
    ("http://signal.example.org", False),
    ("ftp://127.0.0.1", False),
])
def test_daemon_url_must_be_loopback_or_tls(url, ok):
    """CHAT-21: the daemon has no auth — plain HTTP only to loopback."""
    from surfaces.signal.client import SignalClient, SignalEventStream, daemon_url_problem
    assert (daemon_url_problem(url) is None) is ok
    if not ok:
        with _pytest.raises(ValueError):
            SignalClient(url, "+1555")
        with _pytest.raises(ValueError):
            SignalEventStream(url)
