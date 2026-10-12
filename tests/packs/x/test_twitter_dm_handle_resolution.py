"""The DM sweep reads threads by the id the inbox listing gave (prod 2026-10-09/11).

33 failures in 72 h: "Could not resolve participant 'RobinhoodAlphas' /
'MrGemOfficial'", ~2 per DM-sweep run. The sweep lists the X Chat inbox (which
carries each participant's numeric id and @handle), then reads each thread BY
HANDLE; a handle X no longer resolves failed every run, forever, and was never
recorded. Now:

* a handle the inbox listing named resolves to its listed id, no lookup;
* a handle X says does not exist is recorded in the dead-target store (surface
  ``x``) and later reads skip it with one log line and a plain error;
* the OAuth2 lookup failure is logged at WARNING, once per handle.
"""
import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("polyrob_x")

from tools.base_tool import ToolStatus
from polyrob_x import handle_ids as tt
from polyrob_x.twitter_tool import TwitterGetDMsAction, TwitterTool


class _Dead:
    def __init__(self):
        self.rows = {}

    def mark(self, surface, address, reason):
        from core.surfaces.address_key import canonical_addr
        self.rows[(surface, canonical_addr(address))] = (reason, time.time())

    def is_dead(self, surface, address):
        from core.surfaces.address_key import canonical_addr
        return (surface, canonical_addr(address)) in self.rows

    def list_all(self):
        return [{"surface": s, "address": a, "reason": r, "marked_at": m, "fail_count": 1}
                for (s, a), (r, m) in self.rows.items()]

    def clear(self, surface, address):
        from core.surfaces.address_key import canonical_addr
        self.rows.pop((surface, canonical_addr(address)), None)


@pytest.fixture(autouse=True)
def _fresh_cache():
    tt._HANDLE_IDS.clear()
    tt._WARNED_LOOKUPS.clear()
    yield
    tt._HANDLE_IDS.clear()
    tt._WARNED_LOOKUPS.clear()


def _tool(monkeypatch, dead=None):
    monkeypatch.setenv("DEAD_TARGET_REGISTRY", "true")
    t = object.__new__(TwitterTool)
    t.logger = logging.getLogger("tw-dm-resolve")
    t.name = "twitter"
    t._status = ToolStatus.HEALTHY
    t._error_message = None
    t._enabled = True
    t._initialized = True
    container = MagicMock()
    container.get_service.side_effect = lambda name: dead if name == "dead_targets" else None
    t._container = container
    t.container = container
    t._services = {}
    t.client = MagicMock(name="oauth1_client")
    t.dm_client = MagicMock(name="oauth2_client")
    t.oauth2_access_token = None
    t._oauth1_available = True
    monkeypatch.setattr(TwitterTool, "_ensure_oauth2_fresh",
                        lambda self, force_refresh=False: None)
    chat = MagicMock()
    chat.get_conversations = AsyncMock(return_value={
        "data": [{"id": "c1"}, {"id": "c2"}],
        "includes": {"users": [{"id": "1300433847599808514", "username": "RobinhoodAlphas"},
                               {"id": "42", "username": "fanrobinhood"}]},
        "meta": {}})
    chat.read_conversation = AsyncMock(return_value={"messages": []})
    t.chat_client = chat
    return t


@pytest.mark.asyncio
async def test_a_listed_handle_reads_by_its_listed_id(monkeypatch):
    t = _tool(monkeypatch)
    t.get_user_by_identifier = AsyncMock(return_value=None)
    await t.twitter_get_dms(TwitterGetDMsAction(rail="chat"))          # the listing
    res = await t.twitter_get_dms(TwitterGetDMsAction(rail="chat",
                                                      participant="@robinhoodalphas"))
    assert res.error is None, res.error
    t.get_user_by_identifier.assert_not_called()
    assert t.chat_client.read_conversation.call_args.args[0] == "1300433847599808514"


@pytest.mark.asyncio
async def test_a_handle_x_does_not_know_is_recorded_and_skipped(monkeypatch, caplog):
    dead = _Dead()
    t = _tool(monkeypatch, dead)
    t.get_user_by_identifier = AsyncMock(return_value=None)
    res = await t.twitter_get_dms(TwitterGetDMsAction(rail="chat", participant="MrGemOfficial"))
    assert res.error and "MrGemOfficial" in res.error
    assert dead.is_dead("x", "mrgemofficial")
    assert t.get_user_by_identifier.await_count == 1
    caplog.set_level(logging.INFO)
    res = await t.twitter_get_dms(TwitterGetDMsAction(rail="chat", participant="MrGemOfficial"))
    assert res.error and "not found on X" in res.error
    assert "numeric id" in res.error
    assert t.get_user_by_identifier.await_count == 1                   # no second lookup
    assert any("skipped" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_dead_mark_expires_and_a_live_handle_is_cleared(monkeypatch):
    dead = _Dead()
    dead.rows[("x", "mrgemofficial")] = ("no_such_user", time.time() - 8 * 86400)
    t = _tool(monkeypatch, dead)
    t.get_user_by_identifier = AsyncMock(return_value={"id": "77", "username": "MrGemOfficial"})
    res = await t.twitter_get_dms(TwitterGetDMsAction(rail="chat", participant="MrGemOfficial"))
    assert res.error is None, res.error
    assert not dead.is_dead("x", "mrgemofficial")


@pytest.mark.asyncio
async def test_a_numeric_id_is_never_looked_up_or_skipped(monkeypatch):
    dead = _Dead()
    t = _tool(monkeypatch, dead)
    t.get_user_by_identifier = AsyncMock(return_value=None)
    res = await t.twitter_get_dms(TwitterGetDMsAction(rail="chat",
                                                      participant="1321846956432510976"))
    assert res.error is None, res.error
    t.get_user_by_identifier.assert_not_called()


@pytest.mark.asyncio
async def test_a_rate_limit_during_initialization_is_not_a_dead_mark(monkeypatch):
    """_make_request answers None for a rate limit in initialization mode; that
    must not record the handle as dead for days (review 2026-10-11)."""
    dead = _Dead()
    t = _tool(monkeypatch, dead)
    t._services["rate_limit_manager"] = MagicMock(_initialization_mode=True)
    t.get_user_by_identifier = AsyncMock(return_value=None)
    res = await t.twitter_get_dms(TwitterGetDMsAction(rail="chat", participant="SomeHandle"))
    assert res.error
    assert not dead.rows


@pytest.mark.asyncio
async def test_a_lookup_error_is_not_a_dead_mark(monkeypatch):
    dead = _Dead()
    t = _tool(monkeypatch, dead)
    t.get_user_by_identifier = AsyncMock(side_effect=RuntimeError("429 Too Many Requests"))
    res = await t.twitter_get_dms(TwitterGetDMsAction(rail="chat", participant="SomeHandle"))
    assert res.error
    assert not dead.rows


@pytest.mark.asyncio
async def test_the_oauth2_lookup_failure_warns_once_per_handle(monkeypatch, caplog):
    t = _tool(monkeypatch, _Dead())
    t.oauth2_access_token = "tok"
    monkeypatch.setattr(TwitterTool, "_oauth2_relogin_needed", lambda self: False)

    async def _boom(**kw):
        raise RuntimeError("403 Forbidden")
    t._make_request = _boom
    t.get_user_by_identifier = AsyncMock(return_value={"id": "5", "username": "abc"})
    caplog.set_level(logging.WARNING)
    await t._resolve_user_id("abc")
    await t._resolve_user_id("abc")
    warns = [r for r in caplog.records
             if r.levelno == logging.WARNING and "OAuth2 username lookup" in r.getMessage()]
    assert len(warns) == 1
