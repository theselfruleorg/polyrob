"""API-1: an x402 payer's tenant is resolved before settlement, read-both.

A wallet that already holds a SIWE profile (usr_<16hex>) used to be refused
AFTER the facilitator settled (500 + refund-due on every paid request) because
the x402 id (usr_<12hex>) never matched. The wallet's own row now wins; a new
wallet gets the 64-bit id; a legacy 48-bit account keeps its id.
"""
import pytest

from core.identity import generate_user_id_from_wallet, wallet_user_id
from modules.auth.identity_mapper import IdentityMapper
from modules.x402.x402_integration import (
    ensure_user_profile_for_payer, resolve_payer_user_id)

WALLET = "0x" + "Ab" * 20


async def _add(db, user_id, wallet):
    await db.execute(
        "INSERT INTO user_profiles (user_id, wallet_address, role, tier) "
        "VALUES (?, ?, 'user', 'free')", (user_id, wallet.lower()))


def test_new_id_is_64_bit_and_matches_the_siwe_id():
    uid = wallet_user_id(WALLET)
    assert len(uid) == len("usr_") + 16
    assert uid == IdentityMapper(None, None)._generate_deterministic_user_id(WALLET)


@pytest.mark.asyncio
async def test_siwe_profile_wallet_binds_to_its_own_account(x402_db):
    siwe_id = IdentityMapper(None, None)._generate_deterministic_user_id(WALLET)
    await _add(x402_db, siwe_id, WALLET)
    uid = await resolve_payer_user_id(WALLET, db=x402_db)
    assert uid == siwe_id
    assert await ensure_user_profile_for_payer(WALLET, uid, db=x402_db)


@pytest.mark.asyncio
async def test_legacy_48_bit_account_is_not_orphaned(x402_db):
    legacy = generate_user_id_from_wallet(WALLET)
    await _add(x402_db, legacy, WALLET)
    uid = await resolve_payer_user_id(WALLET, db=x402_db)
    assert uid == legacy
    assert await ensure_user_profile_for_payer(WALLET, uid, db=x402_db)


@pytest.mark.asyncio
async def test_new_wallet_gets_the_64_bit_id_and_a_profile(x402_db):
    await x402_db.execute(
        "CREATE TABLE IF NOT EXISTS user_credits (user_id TEXT PRIMARY KEY, "
        "balance INTEGER, lifetime_earned INTEGER, lifetime_spent INTEGER)")
    uid = await resolve_payer_user_id(WALLET, db=x402_db)
    assert uid == wallet_user_id(WALLET)
    assert await ensure_user_profile_for_payer(WALLET, uid, db=x402_db)
    row = await x402_db.fetch_one(
        "SELECT wallet_address FROM user_profiles WHERE user_id = ?", (uid,))
    assert row["wallet_address"] == WALLET.lower()


@pytest.mark.asyncio
async def test_id_held_by_another_wallet_is_refused(x402_db):
    await _add(x402_db, wallet_user_id(WALLET), "0x" + "9" * 40)
    assert await resolve_payer_user_id(WALLET, db=x402_db) is None


@pytest.mark.asyncio
async def test_no_database_refuses(monkeypatch):
    async def _none(db=None):
        return None
    monkeypatch.setattr("modules.x402._db.resolve_db", _none)
    assert await resolve_payer_user_id(WALLET) is None


@pytest.mark.asyncio
async def test_unbindable_wallet_is_refused_before_settlement(monkeypatch):
    import base64
    import json
    from starlette.requests import Request
    from modules.x402 import middleware as M
    monkeypatch.setenv('X402_PAYMENT_RECIPIENT', "0x" + "22" * 20)
    monkeypatch.setenv('X402_DEFAULT_CHAIN', 'base')

    async def _unbound(addr):
        return None
    monkeypatch.setattr(M, 'resolve_payer_user_id', _unbound)

    class _Never:
        async def verify_and_settle_payment(self, *a, **kw):
            pytest.fail("a payer that cannot be bound must not be settled")

    async def downstream(request):
        pytest.fail("no service on a refused payment")

    obj = M.X402PaymentMiddleware(lambda *a: None, enabled=False)
    obj._facilitator_client = _Never()
    header = base64.b64encode(json.dumps({"payload": {"authorization": {
        "from": WALLET, "nonce": "0x" + "44" * 32}}}).encode()).decode()
    request = Request({'type': 'http', 'method': 'POST', 'path': '/a2a/rpc',
                       'headers': [], 'server': ('test', 80), 'scheme': 'http',
                       'query_string': b''})
    response = await obj._handle_x402_payment(request, downstream, header)
    assert response.status_code == 503
    assert b"No payment was taken" in response.body
