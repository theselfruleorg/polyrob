"""Regression (P1/Phase-6 finalization): CLI commands quieted bootstrap noise with
logging.disable(CRITICAL) then "restored" with logging.disable(ERROR) — but ERROR is
not NOTSET, so sub-ERROR logging stayed disabled process-wide forever. In tests this
blinded caplog for everything after (the test_malformed_toml_fails_open bleed); in a
long-lived process it silently drops logs. They must restore to NOTSET.
"""
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from core.identity import ConstantIdentity


def test_session_cancel_restores_logging_disable_to_notset():
    from cli.commands import session as sess

    fake_agent = MagicMock()
    fake_agent.cancel_session = AsyncMock(return_value=True)
    fake_container = MagicMock()
    fake_container.get_agent.return_value = fake_agent
    fake_container.get_service.return_value = ConstantIdentity("t")

    async def _fake_build(*a, **k):
        return fake_container

    logging.disable(logging.NOTSET)  # clean baseline
    with patch("core.bootstrap.build_cli_container", _fake_build), \
         patch("core.bootstrap.setup_project_path", lambda: None), \
         patch("core.bootstrap.setup_sqlite_compat", lambda: None), \
         patch("cli.keys.preflight_or_onboard", lambda **k: True):
        asyncio.run(sess._session_cancel("sess-1"))

    assert logging.Logger.manager.disable == logging.NOTSET, (
        "logging.disable must be restored to NOTSET, not left at ERROR"
    )
