"""DM rails fall back to OAuth 1.0a when the OAuth 2.0 login is dead (2026-09-26).

The OAuth2 refresh token died when the client secret was regenerated; both the
X Chat and the legacy ``/2/dm_events`` rail sent the dead token and 401'd,
although ``/2/dm_events`` and the DM send accept OAuth 1.0a user context and the
OAuth1 keys worked. These tests pin: a 401 on OAuth2 retries with OAuth1; an
open ``x_oauth2`` relogin verdict skips OAuth2 outright; a final 401 names the
remedy; the stale env-centric X Chat string is gone.
"""
import pytest

pytest.importorskip("polyrob_x")

import logging
from unittest.mock import AsyncMock, MagicMock

import tweepy

from tools.base_tool import ToolStatus
from polyrob_x.twitter_tool import TwitterDMAction, TwitterGetDMsAction, TwitterTool


class _Unauth(Exception):
    def __str__(self):
        return "401 Unauthorized\nUnauthorized"


def _tool(monkeypatch, *, oauth1=True, oauth2="oauth2-tok"):
    monkeypatch.setenv("TWITTER_ENABLED", "true")
    monkeypatch.setenv("TWITTER_REQUIRE_APPROVAL", "false")
    t = object.__new__(TwitterTool)
    t.logger = logging.getLogger("tw-fallback")
    t.name = "twitter"
    t._status = ToolStatus.HEALTHY
    t._error_message = None
    t._enabled = True
    t._initialized = True
    t._container = MagicMock()
    t._services = {}
    t.client = MagicMock(name="oauth1_client")
    t.dm_client = MagicMock(name="oauth2_client")
    t.chat_client = None
    t.api_v1 = MagicMock()
    t._write_times = []
    t._dm_times = []
    t.oauth2_access_token = oauth2
    t._oauth1_available = oauth1
    # the store resolver must not rotate the token under the test
    monkeypatch.setattr(TwitterTool, "_ensure_oauth2_fresh", lambda self, force_refresh=False: None)
    return t


def _events():
    ev = MagicMock()
    ev.data = {"id": "1", "text": "inbound", "sender_id": "42", "dm_conversation_id": "42-9"}
    return MagicMock(data=[ev], meta={})


@pytest.mark.asyncio
async def test_legacy_read_401_on_oauth2_retries_with_oauth1(monkeypatch):
    t = _tool(monkeypatch)
    t.dm_client.get_direct_message_events.side_effect = _Unauth()
    t.client.get_direct_message_events.return_value = _events()
    res = await t.twitter_get_dms(TwitterGetDMsAction(rail="legacy"))
    assert res.error is None, res.error
    assert "inbound" in res.extracted_content
    assert '"auth": "oauth1_user"' in res.extracted_content
    assert t.dm_client.get_direct_message_events.call_args.kwargs["user_auth"] is False
    assert t.client.get_direct_message_events.call_args.kwargs["user_auth"] is True


@pytest.mark.asyncio
async def test_open_relogin_verdict_skips_the_dead_oauth2_token(monkeypatch):
    from core.credential_verdicts import record_rejection
    record_rejection("x_oauth2", "", code="relogin_needed", remedy="re-login")
    t = _tool(monkeypatch)
    t.chat_client = MagicMock()
    t.client.get_direct_message_events.return_value = _events()
    res = await t.twitter_get_dms(TwitterGetDMsAction())  # rail=auto
    assert res.error is None, res.error
    t.dm_client.get_direct_message_events.assert_not_called()
    assert '"rail": "legacy"' in res.extracted_content
    assert "relogin" in res.extracted_content and "/x login" in res.extracted_content


@pytest.mark.asyncio
async def test_auto_chat_401_falls_back_to_legacy_oauth1(monkeypatch):
    t = _tool(monkeypatch)
    t.chat_client = MagicMock()
    t.chat_client.get_conversations = AsyncMock(
        side_effect=RuntimeError("X Chat API 401: Unauthorized"))
    t.dm_client.get_direct_message_events.side_effect = _Unauth()
    t.client.get_direct_message_events.return_value = _events()
    res = await t.twitter_get_dms(TwitterGetDMsAction())
    assert res.error is None, res.error
    assert "Fell back to the legacy" in res.extracted_content


@pytest.mark.asyncio
async def test_dm_send_401_on_oauth2_retries_with_oauth1(monkeypatch):
    t = _tool(monkeypatch)
    t.dm_client.create_direct_message.side_effect = _Unauth()
    t.client.create_direct_message.return_value = MagicMock(data={"dm_event_id": "7"})
    res = await t.twitter_dm(TwitterDMAction(recipient="123456", text="hi", allow_plaintext=True))
    assert res.error is None, res.error
    assert t.client.create_direct_message.call_args.kwargs["user_auth"] is True


@pytest.mark.asyncio
async def test_final_401_names_the_remedy(monkeypatch):
    t = _tool(monkeypatch, oauth1=False)
    t.dm_client.create_direct_message.side_effect = _Unauth()
    res = await t.twitter_dm(TwitterDMAction(recipient="123456", text="hi", allow_plaintext=True))
    assert res.error and "401" in res.error
    assert "/x login" in res.error and "polyrob x-account oauth-login" in res.error
    assert "no fallback rail" in res.error


@pytest.mark.asyncio
async def test_a_non_401_error_does_not_switch_credentials(monkeypatch):
    t = _tool(monkeypatch)
    t.dm_client.get_direct_message_events.side_effect = RuntimeError("500 Server Error")
    res = await t.twitter_get_dms(TwitterGetDMsAction(rail="legacy"))
    assert res.error and "/x login" not in res.error
    t.client.get_direct_message_events.assert_not_called()


@pytest.mark.asyncio
async def test_chat_without_a_login_names_the_store_not_the_env(monkeypatch):
    t = _tool(monkeypatch)
    t.chat_client = None
    res = await t.twitter_get_dms(TwitterGetDMsAction(rail="chat"))
    assert res.error
    assert "TWITTER_OAUTH2_ACCESS_TOKEN" not in res.error
    assert "token store" in res.error and "/x login" in res.error


def test_is_unauthorized_sees_through_the_apierror_wrap():
    from core.exceptions import APIError  # noqa: F401 - the wrapper _make_request raises
    try:
        try:
            raise tweepy.errors.TweepyException("401 Unauthorized")
        except Exception as inner:
            raise RuntimeError(f"Twitter request failed: {inner}")
    except RuntimeError as e:
        assert TwitterTool._is_unauthorized(e)
    assert not TwitterTool._is_unauthorized(RuntimeError("403 Forbidden"))
