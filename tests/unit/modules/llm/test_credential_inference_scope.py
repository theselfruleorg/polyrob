import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.inference_context import inference_user
from core.llm_auth.inference import require_credential_access
from modules.llm.adapters import LLMClientAdapter
from modules.llm.messages import HumanMessage


@pytest.fixture(autouse=True)
def owner(monkeypatch):
    monkeypatch.setattr('core.instance.is_owner', lambda uid: uid == 'owner')


@pytest.mark.parametrize('source', ['oauth', 'borrowed'])
@pytest.mark.parametrize('uid', [None, '', 'tenant'])
def test_private_credential_refuses_missing_or_nonowner_identity(source, uid):
    with inference_user(uid), pytest.raises(PermissionError, match='owner session'):
        require_credential_access(SimpleNamespace(_credential_source=source))


def test_cached_oauth_client_without_source_metadata_is_still_private():
    client = SimpleNamespace(_spec=SimpleNamespace(auth_type='oauth_device'))
    with inference_user('tenant'), pytest.raises(PermissionError):
        require_credential_access(client)
    with inference_user('owner'):
        require_credential_access(client)
    with pytest.raises(PermissionError):
        require_credential_access(client)


@pytest.mark.asyncio
async def test_shared_adapter_rechecks_each_call_and_stream_before_provider():
    client = SimpleNamespace(model_type='model', _credential_source='oauth',
                             generate_response=AsyncMock(return_value='ok'))
    adapter = LLMClientAdapter(client=client)
    with inference_user('owner'):
        assert (await adapter.ainvoke([HumanMessage(content='hello')])).content == 'ok'
    with inference_user('tenant'):
        with pytest.raises(PermissionError):
            await adapter.ainvoke([HumanMessage(content='hello')])
        with pytest.raises(PermissionError):
            async for _ in adapter._astream_true([HumanMessage(content='hello')]):
                pass
    assert client.generate_response.await_count == 1


@pytest.mark.asyncio
async def test_parallel_tenants_and_aux_tasks_do_not_share_principal():
    client = SimpleNamespace(_credential_source='borrowed')
    async def read(uid):
        with inference_user(uid):
            await asyncio.sleep(0)
            return await asyncio.to_thread(require_credential_access, client)
    results = await asyncio.gather(read('owner'), read('tenant'), return_exceptions=True)
    assert results[0] is None
    assert isinstance(results[1], PermissionError)


def test_environment_api_keys_keep_shared_service_role():
    with inference_user('tenant'):
        require_credential_access(SimpleNamespace(_credential_source='env'))


def test_paid_sdk_retries_use_local_view_only():
    from core.billing_context import mark_billed, reset_billed
    from modules.llm.billing_guard import inference_sdk
    sdk = Mock()
    sdk.max_retries = 2
    client = SimpleNamespace(_client=sdk, _credential_source='env')
    assert inference_sdk(client) is sdk
    token = mark_billed()
    try:
        assert inference_sdk(client) is sdk.with_options.return_value
        sdk.with_options.assert_called_once_with(max_retries=0)
        assert sdk.max_retries == 2
    finally:
        reset_billed(token)


@pytest.mark.asyncio
async def test_session_runner_sets_and_resets_authenticated_principal():
    from agents.task_agent_lite import TaskAgent
    agent = object.__new__(TaskAgent)
    agent.task_available = True
    agent._session_execution_locks = {}
    agent.session_manager = SimpleNamespace(get_session_info=lambda sid: {
        'user_id': 'owner' if sid == 's' else 'tenant'})
    agent.container = SimpleNamespace(get_service=lambda _: None)
    client = SimpleNamespace(_credential_source='oauth')
    async def run(uid, sid):
        require_credential_access(client)
        return 'done'
    agent._run_session_impl = run
    assert await agent.run_session('owner', 's') == 'done'
    with pytest.raises(PermissionError):
        await agent.run_session('tenant', 's2')
    with pytest.raises(PermissionError):
        require_credential_access(client)
