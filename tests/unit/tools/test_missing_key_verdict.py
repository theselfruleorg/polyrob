"""057 WS-F: a missing API key is a durable VERDICT, warned once per process.

Before WS-F a tool with no key warned on every construction — and a new tool
instance is built per session — while leaving nothing durable, so the operator
got log noise and `/status` had no health line for "perplexity: no key".
"""
import logging
import types

import pytest

from core import credential_verdicts as cv
from tools.perplexity_tool import PerplexityTool


def _cfg(key=None):
    return types.SimpleNamespace(perplexity_api_key=key)


def test_missing_key_records_a_verdict_with_a_remedy():
    PerplexityTool(name="perplexity", config=_cfg(None), container=None)
    v = cv.verdict("missing_key", "perplexity")
    assert v is not None
    assert v.code == "missing_config"
    assert "PERPLEXITY_API_KEY" in v.remedy
    assert v.ttl_sec == cv.MISSING_KEY_TTL_SEC
    assert [x.key for x in cv.active("missing_key")] == ["perplexity"]


def test_the_warning_is_once_per_process_not_once_per_session(caplog):
    with caplog.at_level(logging.DEBUG):
        for _ in range(5):
            PerplexityTool(name="perplexity", config=_cfg(None), container=None)
    warns = [r for r in caplog.records
             if r.levelname == "WARNING" and "missing required configuration" in r.getMessage()]
    assert len(warns) == 1, f"one WARNING per outage, got {len(warns)}"
    # the verdict still COUNTS every construction — the fact is not lost, only the noise
    assert cv.verdict("missing_key", "perplexity").count == 5


def test_a_present_key_clears_the_verdict():
    PerplexityTool(name="perplexity", config=_cfg(None), container=None)
    assert cv.verdict("missing_key", "perplexity") is not None
    PerplexityTool(name="perplexity", config=_cfg("pplx-real"), container=None)
    assert cv.verdict("missing_key", "perplexity") is None


def test_note_missing_config_is_fail_open(monkeypatch, caplog):
    """A broken verdict store must still tell the operator."""
    t = PerplexityTool(name="perplexity", config=_cfg("pplx-real"), container=None)

    def _boom(*a, **k):
        raise RuntimeError("store down")

    monkeypatch.setattr(cv, "record_rejection", _boom)
    monkeypatch.setattr(cv, "warn_once", _boom)
    with caplog.at_level(logging.WARNING):
        t.note_missing_config(["PERPLEXITY_API_KEY"])
    assert any("missing required configuration" in r.getMessage() for r in caplog.records)


def test_no_missing_keys_is_a_no_op(caplog):
    t = PerplexityTool(name="perplexity", config=_cfg("pplx-real"), container=None)
    with caplog.at_level(logging.DEBUG):
        t.note_missing_config([])
    assert cv.active("missing_key") == []


@pytest.mark.parametrize("key", ["", None])
def test_placeholder_and_empty_keys_both_count_as_missing(key):
    PerplexityTool(name="perplexity", config=_cfg(key), container=None)
    assert cv.verdict("missing_key", "perplexity") is not None
