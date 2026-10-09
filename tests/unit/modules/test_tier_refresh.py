import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.exceptions import TierError
from modules.auth.tier_manager import TierManager


class DB:
    def __init__(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('CREATE TABLE user_profiles (user_id TEXT, tier TEXT, wallet_address TEXT, den_token_verified_at TEXT, den_token_count INTEGER)')
        self.conn.execute("INSERT INTO user_profiles VALUES ('u', 'holder', 'wallet', NULL, 1)")

    async def fetch_one(self, query, params):
        row = self.conn.execute(query, params).fetchone()
        return dict(row) if row else None

    async def execute(self, query, params):
        self.conn.execute(query, params)
        self.conn.commit()


@pytest.fixture
def db():
    database = DB()
    yield database
    database.conn.close()


@pytest.mark.asyncio
async def test_transferred_den_loses_access_and_fresh_positive_is_cached(db, monkeypatch):
    check = AsyncMock(return_value={'status': 'success', 'has_token': True, 'token_count': 1})
    monkeypatch.setattr('core.token_check_hook.token_checker', lambda: check)
    manager = TierManager(db, alchemy_tool=object())
    assert await manager.get_user_tier('u') == 'holder'
    assert await manager.get_user_tier('u') == 'holder'
    assert check.await_count == 1
    await db.execute("UPDATE user_profiles SET den_token_verified_at='2000-01-01'", ())
    check.return_value = {'status': 'success', 'has_token': False, 'token_count': 0}
    assert await manager.get_user_tier('u') == 'free'
    assert (await db.fetch_one('SELECT den_token_count FROM user_profiles', ()))['den_token_count'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('result', [None, {'status': 'error', 'has_token': False, 'token_count': 0},
                                    {'status': 'success', 'has_token': True, 'token_count': True}])
async def test_unverifiable_holder_cannot_keep_stale_access(db, monkeypatch, result):
    monkeypatch.setattr('core.token_check_hook.token_checker', lambda: AsyncMock(return_value=result))
    with pytest.raises(TierError, match='unavailable'):
        await TierManager(db, alchemy_tool=object()).get_user_tier('u')


@pytest.mark.asyncio
async def test_missing_checker_fails_closed_but_admin_is_independent(db, monkeypatch):
    monkeypatch.setattr('core.token_check_hook.token_checker', lambda: None)
    manager = TierManager(db)
    with pytest.raises(TierError):
        await manager.get_user_tier('u')
    await db.execute("UPDATE user_profiles SET tier='admin'", ())
    assert await manager.get_user_tier('u') == 'admin'
