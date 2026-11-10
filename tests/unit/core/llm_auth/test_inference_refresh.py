"""U1, U2, U3: a call refreshes first, an expired credential reads as relogin required, and
an unreadable store is not an empty one."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from core.exceptions import LLMConnectionError
from core.inference_context import inference_user
from core.llm_auth.errors import LLMAuthError
from core.llm_auth.flows.base import TRANSIENT_CODE, FlowError
from core.llm_auth.inference import read_credential, refresh_credential
from core.llm_auth.resolve import (HEALTH_STORE_UNREADABLE, SOURCE_STORE_UNREADABLE, resolve_credential)
from core.llm_auth.store import AuthStore, AuthStoreUnreadable
from modules.llm.messages import HumanMessage

async def _first(stream):
    async for chunk in stream:
        return chunk


OAUTH = SimpleNamespace(token_url="https://auth.example.test/token", client_id="c",
                        refresh_skew_sec=120, token_exchange_url="")


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setattr("core.llm_auth.resolve._tenant_allowed", lambda user_id: True)
    with inference_user("owner"):
        yield


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    monkeypatch.setenv("POLYROB_AUTH_STORE", str(path))
    return AuthStore(path)


def _client(token="old", source="oauth"):
    sdk = SimpleNamespace(auth_token=token, api_key=None)
    return SimpleNamespace(_credential_source=source, api_key=token, _client=sdk,
                           _spec=SimpleNamespace(name="seat", oauth=OAUTH))


def _before_the_call(client):
    asyncio.run(refresh_credential(client))
    read_credential(client)


def _post(payload, seen=None):
    def post(url, fields, **kw):
        if seen is not None:
            seen.append(fields)
        return payload
    return post


def _patch_refresh(monkeypatch, http_post):
    import core.llm_auth.flows.refresh as module
    real = module.refresh_token

    def refresh(provider, oauth, *, store, now=None):
        return real(provider, oauth, store=store, now=now, http_post=http_post)

    monkeypatch.setattr(module, "refresh_token", refresh)


def test_a_token_near_expiry_is_refreshed_before_the_call(store, monkeypatch):
    store.set_provider("seat", {"access_token": "old", "refresh_token": "r", "expires_at": time.time() + 5})
    seen = []
    _patch_refresh(monkeypatch, _post({"access_token": "new", "expires_in": 3600}, seen))
    client = _client()
    _before_the_call(client)
    assert len(seen) == 1 and seen[0]["grant_type"] == "refresh_token"
    assert client.api_key == "new" and client._client.auth_token == "new"
    assert store.get_provider("seat")["access_token"] == "new"


def test_the_refresh_does_not_block_the_event_loop(store, monkeypatch):
    store.set_provider("seat", {"access_token": "old", "refresh_token": "r", "expires_at": time.time() + 5})

    def slow(url, fields, **kw):
        time.sleep(0.2)
        return {"access_token": "new", "expires_in": 3600}

    _patch_refresh(monkeypatch, slow)
    ticks = []

    async def main():
        async def ticker():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)
        task = asyncio.ensure_future(ticker())
        await asyncio.sleep(0)
        await refresh_credential(_client())
        task.cancel()

    asyncio.run(main())
    assert len(ticks) >= 5, "the loop stood still during the token call"
    assert store.get_provider("seat")["access_token"] == "new"


def test_the_sync_read_makes_no_token_call(store, monkeypatch):
    store.set_provider("seat", {"access_token": "old", "refresh_token": "r", "expires_at": time.time() + 5})
    seen = []
    _patch_refresh(monkeypatch, _post({"access_token": "new", "expires_in": 3600}, seen))
    client = _client(token="stale")
    read_credential(client)
    assert seen == [] and client.api_key == "old"


def test_a_tenant_call_refreshes_nothing(store, monkeypatch):
    store.set_provider("seat", {"access_token": "old", "refresh_token": "r", "expires_at": time.time() + 5})
    seen = []
    _patch_refresh(monkeypatch, _post({"access_token": "new", "expires_in": 3600}, seen))
    monkeypatch.setattr("core.llm_auth.resolve._tenant_allowed", lambda user_id: False)
    with inference_user("tenant"), pytest.raises(PermissionError):
        asyncio.run(refresh_credential(_client()))
    assert seen == []


def test_an_api_key_client_makes_no_store_read(monkeypatch):
    monkeypatch.setattr("core.llm_auth.store.get_auth_store",
                        lambda: pytest.fail("an env credential has no store read"))
    asyncio.run(refresh_credential(SimpleNamespace(_credential_source="env")))


@pytest.mark.parametrize("call", [
    lambda adapter: adapter._agenerate([HumanMessage(content="x")]),
    lambda adapter: _first(adapter.astream([HumanMessage(content="x")])),
    lambda adapter: _first(adapter._astream_true([HumanMessage(content="x")])),
])
def test_each_adapter_entry_refreshes_before_the_read(call, monkeypatch):
    from modules.llm.adapters import LLMClientAdapter
    order = []

    async def refreshed(client):
        order.append("refresh")

    def checked(adapter):
        order.append("read")
        raise RuntimeError("stop here")

    monkeypatch.setenv("LLM_TOKEN_STREAMING", "1")
    monkeypatch.setattr("modules.llm.billing_guard.fresh_credential", refreshed)
    monkeypatch.setattr("modules.llm.billing_guard.check_adapter", checked)
    client = SimpleNamespace(model_type="m", astream_agent_response=lambda **kw: None)
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(call(LLMClientAdapter(client=client, model_name="m")))
    assert order == ["refresh", "read"]


def test_a_token_far_from_expiry_makes_no_token_call(store, monkeypatch):
    store.set_provider("seat", {"access_token": "old", "refresh_token": "r", "expires_at": time.time() + 9000})
    seen = []
    _patch_refresh(monkeypatch, _post({}, seen))
    _before_the_call(_client())
    assert seen == []


def test_a_token_another_process_refreshed_reaches_the_client(store):
    store.set_provider("seat", {"access_token": "other", "expires_at": time.time() + 9000})
    client = _client()
    _before_the_call(client)
    assert client.api_key == "other" and client._client.auth_token == "other"


def test_a_refused_refresh_of_an_expired_token_reads_as_relogin(store, monkeypatch):
    store.set_provider("seat", {"access_token": "old", "refresh_token": "r", "expires_at": time.time() - 5})
    _patch_refresh(monkeypatch, _post({"error": "invalid_grant"}))
    with pytest.raises(LLMAuthError) as err:
        _before_the_call(_client())
    assert err.value.relogin_required and "polyrob auth add seat" in str(err.value)


def test_an_expired_token_with_no_refresh_token_reads_as_relogin(store):
    store.set_provider("seat", {"access_token": "old", "expires_at": time.time() - 5})
    with pytest.raises(LLMAuthError) as err:
        _before_the_call(_client())
    assert err.value.relogin_required


def test_a_failed_refresh_before_the_expiry_keeps_the_token(store, monkeypatch):
    store.set_provider("seat", {"access_token": "old", "refresh_token": "r", "expires_at": time.time() + 30})
    _patch_refresh(monkeypatch, _post({"error": "invalid_grant"}))
    client = _client()
    _before_the_call(client)
    assert client.api_key == "old"


def test_an_unreachable_token_endpoint_is_transient_not_relogin(store, monkeypatch):
    store.set_provider("seat", {"access_token": "old", "refresh_token": "r", "expires_at": time.time() - 5})

    def down(url, fields, **kw):
        raise FlowError("could not reach it", code=TRANSIENT_CODE)

    _patch_refresh(monkeypatch, down)
    with pytest.raises(LLMConnectionError):
        _before_the_call(_client())


def test_a_removed_credential_reads_as_relogin(store):
    store.set_provider("other", {"access_token": "x"})
    with pytest.raises(LLMAuthError):
        _before_the_call(_client())


def test_an_expired_borrowed_token_reads_as_relogin(store):
    store.set_borrowed("seat", {"access_token": "b", "consented": True, "expires_at": time.time() - 5})
    with pytest.raises(LLMAuthError) as err:
        _before_the_call(_client(token="b", source="borrowed"))
    assert err.value.relogin_required


def test_the_borrowed_rung_flags_an_expired_token(store, monkeypatch):
    monkeypatch.setenv("LLM_AUTH_STORE_ENABLED", "1")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("LLM_CREDENTIAL_BORROW", "1")
    store.set_borrowed("seat", {"access_token": "b", "consented": True, "expires_at": time.time() - 5})
    cred = resolve_credential("seat", SimpleNamespace(oauth=OAUTH, auth_type="oauth_device"), store=store)
    assert cred.relogin_required and cred.expires_at is not None


def test_an_unreadable_store_raises_and_a_write_keeps_its_bytes(store, tmp_path):
    store.path.write_text("{not json")
    with pytest.raises(AuthStoreUnreadable):
        store.load()
    with pytest.raises(AuthStoreUnreadable):
        store.set_provider("seat", {"access_token": "x"})
    assert store.path.read_text() == "{not json"


def test_an_unreadable_store_is_a_distinct_state_for_a_seat(store, monkeypatch):
    monkeypatch.setenv("LLM_AUTH_STORE_ENABLED", "1")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    store.path.write_text(json.dumps([1]))
    cred = resolve_credential("seat", SimpleNamespace(oauth=OAUTH, auth_type="oauth_device"), store=store)
    assert cred.source == SOURCE_STORE_UNREADABLE and cred.health == HEALTH_STORE_UNREADABLE
    assert cred.value is None
    plain = resolve_credential("plain", SimpleNamespace(oauth=None, auth_type="api_key"), store=store)
    assert plain is None


def test_doctor_names_an_unreadable_store(store, monkeypatch):
    from modules.llm.profiles import credential_status
    from modules.llm.provider_spec import get_specs
    seat = next(s.name for s in get_specs() if s.oauth is not None)
    store.path.write_text("{not json")
    env = {"LLM_AUTH_STORE_ENABLED": "1", "POLYROB_LOCAL": "1", "POLYROB_AUTH_STORE": str(store.path)}
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    status = credential_status(env)[seat]
    assert status.present and not status.usable
    assert status.reason == f"credential {HEALTH_STORE_UNREADABLE}"
