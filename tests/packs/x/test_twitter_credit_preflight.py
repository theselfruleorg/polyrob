"""2026-09-19 — the X API answered `402 Payment Required` ("credits depleted") on every
read/follow from 10:24Z; outreach round 15 burned its whole step budget rediscovering it
(52 hits in 15 min) and the self-review asked for a pre-flight. Mirror of the SMTP-535
verdict: a 402 is REMEMBERED (`core.credential_verdicts`, kind "twitter_api") so status
shows it, and the error names the remedy. 2026-11-12 (owner): a 402 NEVER blocks the
tool — every call reaches the API, so a top-up works on the very next call."""
import pytest

pytest.importorskip("polyrob_x")

import logging
from unittest.mock import MagicMock

import pytest

from tools.base_tool import ToolStatus
from polyrob_x.twitter_tool import TwitterTool
from core import credential_verdicts as cv


class _Resp:
    def __init__(self, status):
        self.status_code = status
        self.reason = "Payment Required"
        self.text = '{"detail":"credits depleted"}'


class _HTTPErr(Exception):
    def __init__(self, status):
        super().__init__(f"{status} Payment Required")
        self.response = _Resp(status)


def _tool():
    t = object.__new__(TwitterTool)
    t.logger = logging.getLogger("tw-preflight-test")
    t.name = "twitter"
    t._status = ToolStatus.HEALTHY
    t._error_message = None
    t._enabled = True
    t._initialized = True
    t._container = MagicMock()
    t._services = {}
    t.client = MagicMock()
    t.api_v1 = MagicMock()
    t._write_times = []
    t._dm_times = []
    return t


@pytest.fixture(autouse=True)
def _clean():
    cv._reset_for_tests()
    yield
    cv._reset_for_tests()


@pytest.mark.asyncio
async def test_a_402_is_remembered_and_the_next_call_still_reaches_the_api():
    t = _tool()
    calls = []

    async def _exec(func, *a, **kw):
        calls.append(1)
        raise _HTTPErr(402)

    t._execute_request = _exec
    with pytest.raises(Exception) as ei:
        await t._make_request(lambda: None, "users")
    assert "402" in str(ei.value)
    assert cv.rejected_within("twitter_api", 3600)
    with pytest.raises(Exception) as ei2:
        await t._make_request(lambda: None, "users")
    msg = str(ei2.value)
    assert "credits depleted" in msg.lower() and "402" in msg
    assert "developer.x.com" in msg and "x_browser.x_post" in msg
    assert len(calls) == 2, "a 402 verdict never refuses a call locally"


@pytest.mark.asyncio
async def test_a_top_up_works_on_the_next_call_while_the_verdict_stands():
    t = _tool()
    cv.record_rejection("twitter_api", t._credit_verdict_key(), code="402")

    async def _ok(func, *a, **kw):
        return {"ok": True}
    t._execute_request = _ok
    assert await t._make_request(lambda: None, "post") == {"ok": True}
    assert cv.active("twitter_api") == []


@pytest.mark.asyncio
async def test_a_success_clears_the_verdict():
    t = _tool()
    cv.record_rejection("twitter_api", "users")


    async def _ok(func, *a, **kw):
        return {"ok": True}
    t._execute_request = _ok
    # a remembered 402 refuses first; simulate the window elapsing by clearing,
    # then a real success must leave no verdict behind
    cv.clear_rejection("twitter_api", "users")
    assert await t._make_request(lambda: None, "users") == {"ok": True}
    assert not cv.rejected_within("twitter_api", 3600)


@pytest.mark.asyncio
async def test_other_errors_do_not_set_the_verdict():
    t = _tool()


    async def _boom(func, *a, **kw):
        raise _HTTPErr(500)
    t._execute_request = _boom
    with pytest.raises(Exception):
        await t._make_request(lambda: None, "users")
    assert not cv.rejected_within("twitter_api", 3600)


# --- 057 WS-F: the 402 becomes a DURABLE, visible verdict ---------------------

@pytest.mark.asyncio
async def test_a_402_emits_one_durable_x_api_rejected_event(monkeypatch):
    """Prod logged 317 "402" lines in 24 h and emitted NO event, so no status
    surface could show the rail was dead. The event fires per OUTAGE."""
    events = []
    import core.event_log as evl
    monkeypatch.setattr(evl, "emit", lambda kind, **kw: events.append((kind, kw)))
    t = _tool()

    async def _exec(func, *a, **kw):
        raise _HTTPErr(402)
    t._execute_request = _exec

    for _ in range(3):
        with pytest.raises(Exception):
            await t._make_request(lambda: None, "users")
    assert [k for k, _ in events] == ["x_api_rejected"]
    attrs = events[0][1]["attrs"]
    assert attrs["code"] == "402"
    assert attrs["endpoint"] == "users"
    assert attrs["fallback"] == "x_browser.x_post"
    assert "developer.x.com" in attrs["remedy"]


@pytest.mark.asyncio
async def test_the_402_log_line_is_one_warning_per_outage(caplog):
    t = _tool()

    async def _exec(func, *a, **kw):
        raise _HTTPErr(402)
    t._execute_request = _exec
    with caplog.at_level(logging.DEBUG, logger="tw-preflight-test"):
        for _ in range(4):
            with pytest.raises(Exception):
                await t._make_request(lambda: None, "users")
    warns = [r for r in caplog.records
             if r.levelname == "WARNING" and "402" in r.getMessage()]
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(warns) == 1, f"one WARNING per outage, got {len(warns)}"
    assert errors == [], "a known-dead rail is not an ERROR per call"


@pytest.mark.asyncio
async def test_the_verdict_survives_a_restart():
    t = _tool()

    async def _exec(func, *a, **kw):
        raise _HTTPErr(402)
    t._execute_request = _exec
    with pytest.raises(Exception):
        await t._make_request(lambda: None, "users")
    # a new process: the in-memory caches are gone, the store is not
    cv._FALLBACK.clear()
    cv._WARNED.clear()
    cv._READY.clear()
    assert cv.active("twitter_api"), "status still shows the standing 402"


def test_the_twitter_tool_has_no_credential_gate():
    """A 402 never gates the tool in the catalog nor skips a cron job."""
    from polyrob_x.registration import register_twitter_tool
    from tools.tool_disclosure import CREDENTIAL_GATED_TOOLS
    register_twitter_tool(force=True)
    assert "twitter" not in CREDENTIAL_GATED_TOOLS


def test_the_refusal_result_carries_a_structured_fallback_field():
    """The hint is a FIELD, not only prose — but nothing auto-invokes it."""
    r = _tool().create_action_result(error="Error posting: X API credits depleted (402 …)")
    assert r.metadata["fallback"] == "x_browser.x_post"
    assert r.metadata["rail"] == "x_api" and r.metadata["code"] == "402"
    ok = _tool().create_action_result(extracted_content="fine")
    assert ok.metadata is None


@pytest.mark.asyncio
async def test_a_402_is_one_verdict_per_account_not_per_endpoint():
    """116 W6.3: one credit pool — two endpoints refused are ONE verdict, and a
    success on any endpoint clears it (and the old endpoint-keyed rows)."""
    t = _tool()
    t.api_key = "consumer-key-fixture"

    async def _402(func, *a, **kw):
        raise _HTTPErr(402)
    t._execute_request = _402
    with pytest.raises(Exception):
        await t._make_request(lambda: None, "post")
    key = t._credit_verdict_key()
    assert [v.key for v in cv.active("twitter_api")] == [key]
    assert key.startswith("account#") and "consumer-key-fixture" not in key
    # a success (what `_make_request` calls after a good answer) clears the
    # account's verdict and, once per process, the old endpoint-keyed rows
    cv.record_rejection("twitter_api", "users", code="402")
    TwitterTool._legacy_402_keys_swept = False
    t._clear_credit_402()
    assert cv.active("twitter_api") == []
