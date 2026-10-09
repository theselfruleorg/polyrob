import pytest
from core.billing_context import mark_billed, reset_billed
from agents.task.task_agent_support import SessionRequest
from agents.task.billed_request import validate_billed_request


@pytest.fixture
def billed():
    token = mark_billed()
    try:
        yield
    finally:
        reset_billed(token)


@pytest.mark.parametrize("overrides", [
    {"tools": ["perplexity"]}, {"tools": ["anysite"]}, {"tools": ["mcp"]},
    {"tools": "browser"}, {"max_steps": 1000000}, {"max_steps": 0},
    {"max_steps": True}, {"session_config": {"tools_config": {"browser": {"proxy": "http://evil"}}}},
    {"model": "no-price-for-this-model"},
])
def test_billed_tenant_cannot_expand_operator_limits(billed, overrides):
    args = dict(task="test", model="gpt-5", provider="openai", tools=["browser"], max_steps=8)
    args.update(overrides)
    with pytest.raises(ValueError):
        SessionRequest(**args)


def test_preconstructed_request_is_rechecked(billed):
    request = object.__new__(SessionRequest)
    request.model = "gpt-5"
    request.provider = "openai"
    request.max_steps = 51
    with pytest.raises(ValueError, match="max_steps"):
        validate_billed_request(request)


def test_standard_billed_session_still_works(billed):
    request = SessionRequest(task="test", model="gpt-5", provider="openai")
    validate_billed_request(request)


@pytest.mark.asyncio
async def test_reused_registry_refuses_operator_paid_tool(billed):
    from pydantic import BaseModel
    from tools.controller.registry.service import Registry
    registry = Registry()
    class Params(BaseModel):
        pass
    calls = []
    @registry.action("Paid provider", param_model=Params)
    async def paid(params: Params):
        calls.append(True)
    registry.registry.actions["paid"].tool = "perplexity"
    with pytest.raises(ValueError, match="standard session"):
        await registry.execute_action("paid", {})
    assert not calls


def test_persisted_billing_limit_is_restored_and_scoped():
    from core.billing_context import is_billed
    from agents.task.billed_request import billed_session
    assert not is_billed()
    with billed_session({"billing_limited": True}):
        assert is_billed()
    assert not is_billed()


@pytest.mark.parametrize("info", [None, {}, {"billing_limited": False}, {"billing_limited": "true"}])
def test_legacy_session_cannot_resume_as_unrestricted_billed_compute(billed, info):
    from agents.task.billed_request import billed_session
    with pytest.raises(ValueError, match="create a new session"):
        with billed_session(info):
            pytest.fail("must refuse before agent work")


@pytest.mark.asyncio
@pytest.mark.parametrize('tier', ['free', 'holder', 'x402', None])
async def test_non_http_legacy_session_cannot_skip_reservations(tier):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from agents.task.billed_request import session_compute
    services = {'balance_manager': object(),
                'tier_manager': SimpleNamespace(get_user_tier=AsyncMock(return_value=tier))}
    container = SimpleNamespace(get_service=services.get)
    with pytest.raises(ValueError, match='create a new session'):
        async with session_compute({'user_id': 'payer'}, 'payer', container):
            pytest.fail('legacy session cannot run unrestricted')


@pytest.mark.asyncio
async def test_credit_session_creation_limits_without_http_context():
    from core.billing_context import is_billed
    from agents.task.task_agent_support import build_session_metadata
    request = SessionRequest(task='hello', model='gpt-5', provider='openai', max_steps=51)
    with pytest.raises(ValueError, match='max_steps'):
        validate_billed_request(request, required=True)
    request.max_steps = 5
    validate_billed_request(request, required=True)
    info = build_session_metadata(request, effective_tool_ids=[], public_session=False,
                                  credit_billed=True)
    assert info['billing_limited'] is True
    assert not is_billed()  # Creating one session cannot mark the next caller.


@pytest.mark.asyncio
@pytest.mark.parametrize('headless', [False, True])
async def test_admin_and_headless_sessions_do_not_acquire_credit_liability(headless):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from core.billing_context import is_billed
    from agents.task.billed_request import session_compute
    services = {'balance_manager': object(),
                'tier_manager': SimpleNamespace(get_user_tier=AsyncMock(return_value='admin'))}
    container = None if headless else SimpleNamespace(get_service=services.get)
    async with session_compute({'user_id': 'owner'}, 'owner', container):
        assert not is_billed()


class _Svc:
    def __init__(self, tier):
        self._tier = tier

    async def get_user_tier(self, user_id):
        return self._tier


class _Container:
    def __init__(self, tier="free"):
        self._services = {"balance_manager": object(), "tier_manager": _Svc(tier)}

    def get_service(self, name):
        return self._services.get(name)


@pytest.mark.asyncio
async def test_owner_tenant_is_never_a_billed_tenant(monkeypatch):
    """Regression: with credits on (ENABLE_AUTH), the owner's own chat, cron and
    goal sessions (seeded at tier 'free') must not become billed tenants."""
    from agents.task.billed_request import credit_billing_required
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-1")
    assert await credit_billing_required(_Container("free"), "owner-1") is False
    assert await credit_billing_required(_Container("free"), "stranger") is True
