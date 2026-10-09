import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from core.billing_context import mark_billed, reset_billed
from core.exceptions import InsufficientCreditsError
from modules.credits.balance_manager import CreditBalanceManager
from modules.credits.reservations import credit_reserver, settle_reservation
from modules.database.connection import DatabaseConnection


@pytest_asyncio.fixture
async def balance(tmp_path, monkeypatch):
    conn = DatabaseConnection(tmp_path / 'credits.db')
    await conn.connect()
    for sql in (
        "CREATE TABLE user_credits (user_id TEXT PRIMARY KEY, balance INTEGER, "
        "lifetime_earned INTEGER DEFAULT 0, lifetime_spent INTEGER DEFAULT 0, updated_at TEXT)",
        "CREATE TABLE credit_transactions (id INTEGER PRIMARY KEY, user_id TEXT, amount INTEGER, "
        "transaction_type TEXT, reason TEXT, session_id TEXT, balance_before INTEGER, "
        "balance_after INTEGER, timestamp TEXT)",
        "CREATE TABLE billing_failures (id INTEGER PRIMARY KEY, user_id TEXT, status TEXT, "
        "session_id TEXT, request_id TEXT UNIQUE, credits_owed INTEGER, api_cost_usd REAL, "
        "model TEXT, created_at TEXT, resolved_at TEXT, resolution_notes TEXT)",
        "CREATE TABLE usage_records (id INTEGER PRIMARY KEY, user_id TEXT, session_id TEXT, "
        "resource_type TEXT, cost INTEGER, input_tokens INTEGER, output_tokens INTEGER, "
        "cached_tokens INTEGER, cache_creation_tokens INTEGER, api_cost_usd REAL, "
        "markup_multiplier REAL, request_id TEXT UNIQUE, metadata TEXT, timestamp TEXT)",
        "INSERT INTO user_credits (user_id, balance, lifetime_earned) VALUES ('payer', 100, 100)",
    ):
        await conn.execute(sql)
    db = SimpleNamespace(connection=conn, execute=conn.execute,
                         fetch_one=conn.fetch_one, fetch_all=conn.fetch_all)
    monkeypatch.setattr('modules.credits.reservations.reservation_credits', lambda *_: 60)
    yield CreditBalanceManager(db)
    await conn.close()


@pytest.mark.asyncio
async def test_concurrent_calls_cannot_reuse_balance(balance):
    reserve = credit_reserver(balance, 'payer')
    outcomes = await asyncio.gather(*(reserve('model', 'provider') for _ in range(6)),
                                    return_exceptions=True)
    assert sum(isinstance(x, str) for x in outcomes) == 1
    assert (await balance.get_balance('payer'))['balance'] == 40
    rows = await balance.db.fetch_all('SELECT * FROM credit_transactions')
    assert len(rows) == 1 and rows[0]['transaction_type'] == 'reservation'


@pytest.mark.asyncio
async def test_settlement_durable_idempotent_and_payer_bound(balance):
    ref = await credit_reserver(balance, 'payer', 's')('model', 'provider')
    assert not await settle_reservation(balance, 'another-user', ref, 12)
    assert await settle_reservation(balance, 'payer', ref, 12)
    # A fresh manager (no process-local dedup) cannot bill twice.
    assert await settle_reservation(CreditBalanceManager(balance.db), 'payer', ref, 12)
    assert not await settle_reservation(balance, 'payer', ref, 13)
    state = await balance.get_balance('payer')
    assert state == {'balance': 88, 'lifetime_earned': 100, 'lifetime_spent': 12}
    rows = await balance.db.fetch_all('SELECT * FROM credit_transactions')
    assert len(rows) == 3
    assert sum(r['amount'] for r in rows) == -12
    assert [r['amount'] for r in rows if r['transaction_type'] == 'usage'] == [-12]
    assert all(r['session_id'] == 's' for r in rows)


@pytest.mark.asyncio
async def test_zero_cost_releases_hold_and_uncovered_overage_does_not(balance):
    ref = await credit_reserver(balance, 'payer')('model', 'provider')
    assert not await settle_reservation(balance, 'payer', ref, 101)
    assert (await balance.get_balance('payer'))['balance'] == 40
    assert await settle_reservation(balance, 'payer', ref, 0)
    assert (await balance.get_balance('payer'))['balance'] == 100


@pytest.mark.asyncio
async def test_worker_loop_uses_database_owning_loop(balance):
    reserve = credit_reserver(balance, 'payer')
    ref = await asyncio.to_thread(lambda: asyncio.run(reserve('model', 'provider')))
    assert await settle_reservation(balance, 'payer', ref, 10)


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_reservation_precedes_provider_and_travels_on_response(balance, monkeypatch, stream):
    from modules.llm.adapters import LLMClientAdapter
    from modules.llm.aux_metering import extract_stable_request_id
    from modules.llm.messages import HumanMessage
    monkeypatch.setattr('modules.llm.billing_guard.check_adapter', lambda _: None)

    class Client:
        model_type = 'model'
        calls = 0

        async def generate_response(self, **kwargs):
            assert (await balance.get_balance('payer'))['balance'] == 40
            self.calls += 1
            return 'result'

        async def astream_agent_response(self, **kwargs):
            await self.generate_response(**kwargs)
            yield {'type': 'final', 'content': 'result', 'usage_data': {}}

    client = Client()
    adapter = LLMClientAdapter(client=client)
    token = mark_billed(credit_reserver(balance, 'payer'))
    try:
        if stream:
            messages = [m async for m in adapter._astream_true([HumanMessage(content='hello')])]
            response = messages[-1]
        else:
            response = (await adapter._agenerate([HumanMessage(content='hello')])).generations[0].message
        ref = extract_stable_request_id(adapter, response, 'provider')
        assert ref.startswith('llm-reserve:')
        with pytest.raises(InsufficientCreditsError):
            await adapter._agenerate([HumanMessage(content='another call')])
        assert client.calls == 1
        assert await settle_reservation(balance, 'payer', ref, 7)
        assert (await balance.get_balance('payer'))['balance'] == 93
    finally:
        reset_billed(token)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [TimeoutError, asyncio.CancelledError])
async def test_failed_or_cancelled_provider_retains_hold(balance, monkeypatch, failure):
    from modules.llm.adapters import LLMClientAdapter
    from modules.llm.messages import HumanMessage
    monkeypatch.setattr('modules.llm.billing_guard.check_adapter', lambda _: None)

    class Client:
        model_type = 'model'

        async def generate_response(self, **kwargs):
            raise failure('unknown outcome')

    token = mark_billed(credit_reserver(balance, 'payer'))
    try:
        with pytest.raises(BaseException):
            await LLMClientAdapter(client=Client())._agenerate([HumanMessage(content='hello')])
        assert (await balance.get_balance('payer'))['balance'] == 40
        with pytest.raises(InsufficientCreditsError):
            await credit_reserver(balance, 'payer')('model', 'provider')
    finally:
        reset_billed(token)


def test_full_context_bound_covers_cache_write_and_output():
    from modules.credits.pricing import PricingConfig
    from modules.credits.reservations import reservation_credits
    from modules.llm.model_registry import get_model_config_exact
    config = get_model_config_exact('claude-sonnet-4-5')
    cost = (config.context_window * config.pricing.cache_write_price_1h
            + config.max_completion_tokens * config.pricing.output_price) / 1_000_000
    expected, _ = PricingConfig.calculate_credits_from_api_cost(cost)
    assert reservation_credits(config.name, 'anthropic') >= expected


@pytest.mark.asyncio
async def test_usage_tracker_settles_once_across_restart(balance):
    from modules.credits.usage_tracker import LLMUsageTracker
    ref = await credit_reserver(balance, 'payer', 's')('model', 'provider')
    kwargs = dict(user_id='payer', session_id='s', agent_id='a', model='gpt-5',
                  provider='openai', input_tokens=1000, output_tokens=100, request_id=ref)
    first = await LLMUsageTracker(balance.db, balance, None).record_llm_usage(**kwargs)
    again = await LLMUsageTracker(balance.db, balance, None).record_llm_usage(**kwargs)
    assert first.costs.credits_charged == again.costs.credits_charged > 0
    assert (await balance.get_balance('payer'))['balance'] == 100 - first.costs.credits_charged
    assert len(await balance.db.fetch_all('SELECT * FROM usage_records')) == 1
    assert len(await balance.db.fetch_all("SELECT * FROM credit_transactions WHERE transaction_type = 'usage'")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('resolution', ['charged', 'written_off', 'refunded'])
async def test_admin_reconciliation_settles_original_hold(balance, monkeypatch, resolution):
    from fastapi import Request
    from unittest.mock import AsyncMock
    from api.admin_endpoints import resolve_billing_failure, ResolveBillingFailureRequest
    from modules.credits.usage_tracker import LLMUsageTracker
    ref = await credit_reserver(balance, 'payer', 's')('model', 'provider')
    tracker = LLMUsageTracker(balance.db, balance, None)
    # Provider reports a charge exceeding the conservative reservation.
    with pytest.raises(InsufficientCreditsError):
        await tracker.record_llm_usage(user_id='payer', session_id='s', agent_id='a',
            model='gpt-5', provider='openai', input_tokens=1, output_tokens=1,
            billed_cost_usd=2.0, request_id=ref)
    failure = await balance.db.fetch_one('SELECT * FROM billing_failures')
    assert failure and failure['credits_owed'] > 100
    assert (await balance.get_balance('payer'))['balance'] == 40
    # Deposit enough to reconcile. No synthetic second charge may bypass the hold.
    await balance.add_credits('payer', 1000, 'deposit')
    monkeypatch.setattr('api.dependencies.require_service',
                        lambda name, **kw: balance if name == 'balance_manager' else balance.db)
    monkeypatch.setattr('api.admin_endpoints.get_audit_logger', AsyncMock(return_value=None))
    request = Request({'type': 'http', 'method': 'POST', 'headers': [], 'state': {'user_id': 'admin'}})
    await resolve_billing_failure(request, failure['id'],
                                  ResolveBillingFailureRequest(resolution=resolution))
    charge = failure['credits_owed'] if resolution == 'charged' else 0
    assert (await balance.get_balance('payer'))['balance'] == 1100 - charge
    assert await settle_reservation(balance, 'payer', ref, charge)
    assert (await balance.get_balance('payer'))['balance'] == 1100 - charge


@pytest.mark.asyncio
async def test_non_http_session_recreates_reservation_context(balance):
    from agents.task.billed_request import session_compute
    from core.billing_context import reserve_billed_call, is_billed
    container = SimpleNamespace(get_service=lambda name: balance if name == 'balance_manager' else None)
    info = {'user_id': 'payer', 'session_id': 's', 'billing_limited': True}
    async with session_compute(info, 'payer', container):
        ref = await reserve_billed_call('model', 'provider')
        assert (await balance.get_balance('payer'))['balance'] == 40
    assert not is_billed()
    assert await settle_reservation(balance, 'payer', ref, 12)
