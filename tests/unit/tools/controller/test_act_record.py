"""122 W1: the act record — one ``external_write`` row per act with its reason, its short
params and who decided it; never the content of the post."""
import types

import pytest

import core.event_log as el
from core import act_reason


class _Controller:
    _is_sub_agent = False
    session_id = "act-record-s1"  # unique: another test may mark "s1" as a job run

    def __init__(self, tool):
        self._tool = tool

    def get_action_details(self, name):
        return types.SimpleNamespace(tool=self._tool, function=None)


class _Forged:
    user_id = "u1"
    session_id = "act-record-s1"  # unique: another test may mark "s1" as a job run
    role = "leaf"
    is_sub_agent = False
    metadata = {"turn_kind": "self_wake"}


@pytest.fixture
def tlog(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_ENABLED", "true")
    el._INSTANCES.clear()
    yield lambda: el.get_event_log()
    el._INSTANCES.clear()


@pytest.mark.asyncio
async def test_an_act_records_its_reason_params_and_source(tlog):
    from tools.controller.effect_hooks import make_effect_record_hook
    await make_effect_record_hook(_Controller("twitter"))(
        "twitter_reply", {"tweet_id": "42", "text": "a secret plan", "why": "they asked"},
        types.SimpleNamespace(error=None, extracted_content="ok"), _Forged())
    a = tlog().query(kind="external_write")[0]["attrs"]
    assert a["why"] == "they asked"
    assert a["source"] == "agent"
    assert '"tweet_id": "42"' in a["params"] and "a secret plan" not in a["params"]


@pytest.mark.asyncio
async def test_the_bound_reason_and_source_fill_a_verb_with_no_why_param(tlog):
    from tools.controller.effect_hooks import make_effect_record_hook
    why, src = act_reason.set_act_why("the owner's income rule"), act_reason.set_act_source("income rule r1")
    try:
        await make_effect_record_hook(_Controller("twitter"))(
            "twitter_post", {"text": "x"}, types.SimpleNamespace(error=None), _Forged())
    finally:
        act_reason.reset_act_why(why)
        act_reason.reset_act_source(src)
    a = tlog().query(kind="external_write")[0]["attrs"]
    assert a["why"] == "the owner's income rule" and a["source"] == "income rule r1"


def test_the_auto_approved_line_names_the_lane_and_the_reason():
    from tools.controller.approval_queue import _auto_ran_text
    text = _auto_ran_text("email_send", {"why": "the owner's weekly note"})
    assert "lane: reported" in text and "why: the owner's weekly note" in text
    assert "why: not given" in _auto_ran_text("email_send", {})
