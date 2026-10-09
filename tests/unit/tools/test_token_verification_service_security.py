"""Token-verification services are not a model-facing credential probe."""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools.alchemy.alchemy_tool import AlchemyTool, CheckTokenParams
from tools.collabland.collabland_tool import CollabLandTool


def test_internal_services_have_no_decorated_actions():
    for cls in (AlchemyTool, CollabLandTool):
        assert not [name for name in dir(cls) if hasattr(getattr(cls, name), 'action_info')
                    or hasattr(getattr(cls, name), '_description')]


@pytest.mark.asyncio
async def test_alchemy_error_omits_credential_from_result_and_log(caplog):
    secret = 'provider-secret-in-error'
    tool = object.__new__(AlchemyTool)
    tool.logger = logging.getLogger('alchemy-security-test')
    tool.ensure_initialized = AsyncMock()
    tool._enabled = True
    tool._default_contract_address = '0x' + '2' * 40
    tool._check_nft_ownership = AsyncMock(side_effect=RuntimeError(secret))
    result = await tool.alchemy_check_token(CheckTokenParams(address='0x' + '1' * 40))
    assert result['status'] == 'error'
    assert secret not in repr(result) + caplog.text
    assert caplog.records
