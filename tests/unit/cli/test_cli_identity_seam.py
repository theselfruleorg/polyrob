"""P3 — CLI commands resolve the tenant via the IdentityProvider seam, not a hardcoded
"local" (findings F3). A swapped identity must change the tenant the command uses, so
identity resolution has a single source of truth instead of drifting string literals.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from core.identity import ConstantIdentity


def test_session_cancel_resolves_user_via_identity_seam():
    from cli.commands import session as sess

    fake_agent = MagicMock()
    fake_agent.cancel_session = AsyncMock(return_value=True)
    fake_container = MagicMock()
    fake_container.get_agent.return_value = fake_agent
    fake_container.get_service.return_value = ConstantIdentity("tenantZ")

    async def _fake_build(*a, **k):
        return fake_container

    with patch("core.bootstrap.build_cli_container", _fake_build), \
         patch("core.bootstrap.setup_project_path", lambda: None), \
         patch("core.bootstrap.setup_sqlite_compat", lambda: None), \
         patch("cli.keys.preflight_or_onboard", lambda **k: True):
        asyncio.run(sess._session_cancel("sess-1"))

    fake_container.get_service.assert_any_call("identity")
    fake_agent.cancel_session.assert_awaited_once()
    assert fake_agent.cancel_session.call_args.kwargs["user_id"] == "tenantZ"
