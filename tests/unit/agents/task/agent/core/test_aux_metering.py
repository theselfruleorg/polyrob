"""Tests for the aux-LLM metering helper (A3).

Covers: normal metering call records with the right component/tokens; no-op when
no user_id/tracker; fail-open (never raises) on a tracker error including anything
that looks like InsufficientCreditsError.
"""
import asyncio
from types import SimpleNamespace

from modules.llm.aux_metering import meter_aux_llm


class _Tracker:
    def __init__(self):
        self.calls = []

    async def record_llm_usage(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace()


def test_meter_aux_records_with_component_and_tokens():
    tr = _Tracker()
    llm = SimpleNamespace(model_name="claude-haiku-4-5")
    resp = SimpleNamespace(usage_metadata={"input_tokens": 500, "output_tokens": 40})
    asyncio.run(meter_aux_llm(usage_tracker=tr, user_id="u1", session_id="s1",
                              agent_id="a1", llm=llm, response=resp, duration_seconds=0.1,
                              component="compaction", purpose="compaction"))
    assert len(tr.calls) == 1
    c = tr.calls[0]
    assert c["component"] == "compaction" and c["input_tokens"] == 500 and c["output_tokens"] == 40


def test_meter_aux_noop_without_user():
    tr = _Tracker()
    asyncio.run(meter_aux_llm(usage_tracker=tr, user_id=None, session_id="s", agent_id="a",
                              llm=SimpleNamespace(), response=SimpleNamespace(), duration_seconds=0,
                              component="judge", purpose="x"))
    assert tr.calls == []


def test_meter_aux_fails_open_on_tracker_error():
    class Boom:
        async def record_llm_usage(self, **kw):
            raise RuntimeError("insufficient")

    # must NOT raise
    asyncio.run(meter_aux_llm(usage_tracker=Boom(), user_id="u", session_id="s", agent_id="a",
                              llm=SimpleNamespace(model_name="m"), response=SimpleNamespace(usage_metadata={}),
                              duration_seconds=0, component="reflection", purpose="x"))


def test_meter_aux_forwards_cache_creation_tokens():
    """F17: the aux path already EXTRACTED cache_creation_tokens and then dropped
    it — so an aux call's cache write was billed by calculate_cost but never
    recorded on the row, and no rollup could see it."""
    tr = _Tracker()
    llm = SimpleNamespace(model_name="claude-haiku-4-5")
    resp = SimpleNamespace(usage_metadata={
        "input_tokens": 9000,
        "output_tokens": 40,
        "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 7500,
    })
    asyncio.run(meter_aux_llm(usage_tracker=tr, user_id="u1", session_id="s1",
                              agent_id="a1", llm=llm, response=resp, duration_seconds=0.1,
                              component="compaction", purpose="compaction"))
    c = tr.calls[0]
    assert c["cached_tokens"] == 1000
    assert c["cache_creation_tokens"] == 7500


def test_meter_aux_sends_zero_when_the_provider_reports_no_cache_write():
    tr = _Tracker()
    llm = SimpleNamespace(model_name="claude-haiku-4-5")
    resp = SimpleNamespace(usage_metadata={"input_tokens": 500, "output_tokens": 40})
    asyncio.run(meter_aux_llm(usage_tracker=tr, user_id="u1", session_id="s1",
                              agent_id="a1", llm=llm, response=resp, duration_seconds=0.1,
                              component="judge", purpose="x"))
    assert tr.calls[0]["cache_creation_tokens"] == 0
