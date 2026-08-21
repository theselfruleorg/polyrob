"""Regression tests for the TwitterTool lifecycle defects behind the
2026-08-21 outage (the 2026-08-21 singleton-teardown incident):

- S5: the credential check iterated get_twitter_config()'s ALREADY-FILTERED
  dict (empty values removed before the loop looked for empties), so a deploy
  with missing credentials reported the tool enabled/healthy.
- Link 4 / S3: _cleanup() nulled the client but left _initialized True, so the
  `if not tool.is_initialized` re-init gate never fired and the tool stayed
  dead until process restart. _cleanup() must leave the flag consistent with
  the destroyed state even when called directly.
- S5/S6: _check_ready() asserted only static config (_enabled + flag), never
  the live client — a dead client passed the readiness gate and surfaced as an
  untyped AttributeError deep in tweepy.
"""
from unittest.mock import MagicMock

import pytest

from tools.twitter_tool import TwitterTool

_FULL_CREDS = {
    "api_key": "k",
    "api_secret": "s",
    "access_token": "t",
    "access_token_secret": "ts",
    "bearer_token": "b",
}


def _make_tool(creds):
    config = MagicMock()
    config.get_twitter_config.return_value = dict(creds)
    return TwitterTool(name="twitter", config=config, container=MagicMock())


def test_missing_credentials_disable_the_tool():
    """get_twitter_config() filters empty values out BEFORE returning, so the
    tool must check the EXPECTED key set — a partial dict means disabled."""
    tool = _make_tool({"api_key": "k", "bearer_token": "b"})  # 3 of 5 missing
    assert tool._enabled is False


def test_no_credentials_disable_the_tool():
    tool = _make_tool({})
    assert tool._enabled is False


def test_full_credentials_enable_the_tool():
    tool = _make_tool(_FULL_CREDS)
    assert tool._enabled is True


@pytest.mark.asyncio
async def test_private_cleanup_clears_initialized_flag():
    """Even when _cleanup() is invoked directly (bypassing cleanup()), the
    tool must not claim initialized over a destroyed client — that flag is the
    only re-init gate in load_tools_from_container."""
    tool = _make_tool(_FULL_CREDS)
    tool.client = object()
    tool._initialized = True

    await tool._cleanup()

    assert tool.client is None
    assert tool._initialized is False


def test_check_ready_reports_dead_client(monkeypatch):
    """With credentials present and the write surface on, a None client must
    fail readiness with a named cause — not pass and explode as an untyped
    AttributeError inside tweepy."""
    monkeypatch.setenv("TWITTER_ENABLED", "true")
    tool = _make_tool(_FULL_CREDS)
    tool.client = None

    result = tool._check_ready()

    assert result is not None, "_check_ready passed a dead client"
    assert "client" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_ensure_initialized_rebuilds_dead_client(monkeypatch):
    """Self-heal: a torn-down client (client None) must be rebuilt on the next
    read-path call even if a stale flag claims initialized."""
    from unittest.mock import AsyncMock
    import tools.twitter_tool as tt

    tool = _make_tool(_FULL_CREDS)
    monkeypatch.setattr(tt.tweepy, "Client", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(TwitterTool, "_test_api_connection", AsyncMock())
    monkeypatch.setattr(TwitterTool, "_init_v1_client", MagicMock())
    tool.client = None
    tool._initialized = True  # stale flag over a dead client

    await tool._ensure_initialized()

    assert tool.client is not None, "_ensure_initialized left a dead client dead"


@pytest.mark.asyncio
async def test_ensure_initialized_marks_initialized_and_does_not_rebuild(monkeypatch):
    """After a successful ensure, the flag must be set so every later call is
    a no-op — the old code never set it, rebuilding the tweepy client and
    burning a live get_me API call on EVERY search/get_user/get_tweets."""
    from unittest.mock import AsyncMock
    import tools.twitter_tool as tt

    tool = _make_tool(_FULL_CREDS)
    client_factory = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(tt.tweepy, "Client", client_factory)
    monkeypatch.setattr(TwitterTool, "_test_api_connection", AsyncMock())
    monkeypatch.setattr(TwitterTool, "_init_v1_client", MagicMock())
    tool._initialized = False
    tool.client = None

    await tool._ensure_initialized()
    assert tool._initialized is True
    await tool._ensure_initialized()
    await tool._ensure_initialized()

    assert client_factory.call_count == 1, "repeat calls must not rebuild the client"
