"""067 P0.11: the identity mapper reaches the Alchemy NFT check via a hook."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core import token_check_hook
from modules.auth.identity_mapper import IdentityMapper


@pytest.fixture
def restore_checker():
    saved = token_check_hook._checker
    yield
    token_check_hook._checker = saved


def test_alchemy_import_registers_the_checker(restore_checker):
    import tools.alchemy.alchemy_tool as at

    assert token_check_hook.token_checker() is at._check_token_for_identity_mapper


def test_registered_checker_builds_the_params_and_calls_the_tool(restore_checker):
    import tools.alchemy.alchemy_tool as at

    tool = MagicMock()
    tool.alchemy_check_token = AsyncMock(return_value={"token_count": 2})
    out = asyncio.run(at._check_token_for_identity_mapper(tool, "0xabc"))
    assert out == {"token_count": 2}
    (params,), _ = tool.alchemy_check_token.call_args
    assert isinstance(params, at.CheckTokenParams) and params.address == "0xabc"


def test_no_registration_leaves_the_tier_alone(restore_checker):
    token_check_hook._checker = None
    db = MagicMock()
    db.fetch_one = AsyncMock()
    db.execute = AsyncMock()
    mapper = IdentityMapper(db, MagicMock(), alchemy_tool=MagicMock())
    asyncio.run(mapper._update_tier("u1", "0xabc"))
    db.fetch_one.assert_not_called()
    db.execute.assert_not_called()


def test_registered_checker_drives_the_tier(restore_checker):
    seen = []

    async def fake(tool, addr):
        seen.append((tool, addr))
        return {"has_token": False, "token_count": 0}

    token_check_hook.register_token_checker(fake)
    db = MagicMock()
    db.fetch_one = AsyncMock(return_value={"tier": "free"})
    db.execute = AsyncMock()
    tool = object()
    mapper = IdentityMapper(db, MagicMock(), alchemy_tool=tool)
    asyncio.run(mapper._update_tier("u1", "0xabc"))
    assert seen == [(tool, "0xabc")]
    args = db.execute.call_args[0][1]
    assert args == ("free", 0, "u1")
