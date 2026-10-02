"""A refused money verb keeps the rest of that run off every public rail.

Prod 2026-09-26 04:02: the PNL buyback's ``defi_trade_swap`` was refused by the
identity gate, and the same cron run then posted the refusal internals to a
Telegram channel, a group and X. The owner rule — refusals go to the owner
only — lived in skill text. These tests pin it in code:

- a money verb that returns an error (or is vetoed by a pre-hook) taints the run;
- a tainted run's public / non-owner sends are refused by name;
- a message to the OWNER still goes;
- an untainted run (a clean, confirmed tranche) may still post publicly;
- the taint is per run (session), not global, and a genuine owner turn clears it.
"""
import asyncio
import types

import pytest

from core.security import refusal_taint
import tools.controller.refusal_taint_gate as gate

OWNER_TG = "28436760"
OWNER_EMAIL = "owner@example.org"

_TOOLS = {
    "defi_trade_swap": "defi_trade",
    "launchpad_buy": "launchpad",
    "defi_data_swap_quote": "defi_data",
    "twitter_post": "twitter",
    "twitter_get_timeline": "twitter",
    "x_post": "x_browser",
    "email_send": "email",
    "message": None,
    "send_message": None,
    "done": None,
    "owner_ask": None,
}


def _controller(chat_key=None):
    def get_action_details(name):
        return types.SimpleNamespace(tool=_TOOLS.get(name), function=None)
    orch = types.SimpleNamespace(_chat_session_key=chat_key)
    return types.SimpleNamespace(get_action_details=get_action_details,
                                 container=None, user_id="rob", orchestrator=orch)


def _ctx(session_id="run-1"):
    return types.SimpleNamespace(session_id=session_id, user_id="rob", metadata={})


def _err(text="refused: two contracts claim the PNL ticker"):
    return types.SimpleNamespace(error=text)


def _ok():
    return types.SimpleNamespace(error=None)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    refusal_taint.reset_for_tests()
    monkeypatch.setattr(gate, "_owner_targets",
                        lambda controller, user_id: {"telegram": OWNER_TG,
                                                     "email": OWNER_EMAIL})
    yield
    refusal_taint.reset_for_tests()


def run(coro):
    return asyncio.run(coro)


def _taint(controller, session_id="run-1"):
    post = gate.make_refusal_taint_record_hook(controller)
    run(post("defi_trade_swap", {"chain": "robinhood"}, _err(), _ctx(session_id)))


# --------------------------------------------------------------------------
# what taints a run
# --------------------------------------------------------------------------

def test_refused_money_verb_taints_the_run():
    c = _controller()
    _taint(c)
    assert refusal_taint.is_tainted("run-1")


def test_successful_money_verb_does_not_taint():
    c = _controller()
    post = gate.make_refusal_taint_record_hook(c)
    run(post("defi_trade_swap", {}, _ok(), _ctx()))
    assert not refusal_taint.is_tainted("run-1")


def test_failed_read_verb_does_not_taint():
    c = _controller()
    post = gate.make_refusal_taint_record_hook(c)
    run(post("defi_data_swap_quote", {}, _err("rpc timeout"), _ctx()))
    run(post("twitter_get_timeline", {}, _err("402"), _ctx()))
    assert not refusal_taint.is_tainted("run-1")


def test_pre_hook_veto_of_a_money_verb_taints_the_run():
    c = _controller()
    gate.note_denied_action(c, "launchpad_buy", _ctx(), "blocked by the wallet authority")
    assert refusal_taint.is_tainted("run-1")
    gate.note_denied_action(c, "twitter_post", _ctx("run-2"), "paused")
    assert not refusal_taint.is_tainted("run-2")


def test_sub_agent_refusal_taints_the_parent_run():
    c = _controller()
    _taint(c, session_id="run-1__sub_agent_3")
    assert refusal_taint.is_tainted("run-1")


def test_taint_emits_one_event(monkeypatch):
    seen = []
    monkeypatch.setattr(refusal_taint, "_emit",
                        lambda **kw: seen.append(kw))
    c = _controller()
    _taint(c)
    _taint(c)
    assert len(seen) == 1
    assert seen[0]["session_id"] == "run-1"
    assert seen[0]["action"] == "defi_trade_swap"


# --------------------------------------------------------------------------
# what a tainted run may still send
# --------------------------------------------------------------------------

@pytest.mark.parametrize("action,params", [
    ("twitter_post", {"text": "No buyback this cycle. My swap guard refused the order."}),
    ("x_post", {"text": "No buyback this cycle."}),
    ("message", {"surface": "telegram", "target": "telegram:@example_channel",
                 "text": "Buyback cycle 04:02 UTC - no tranche."}),
    ("message", {"surface": "telegram", "target": "-1002125904710",
                 "text": "Buyback cycle 04:02 UTC - no tranche."}),
    ("email_send", {"to": "stranger@example.com", "subject": "x", "body": "y"}),
])
def test_tainted_run_public_send_is_refused(action, params):
    c = _controller()
    _taint(c)
    pre = gate.make_refusal_taint_gate_hook(c)
    reason = run(pre(action, params, _ctx()))
    assert reason and "refused in this run" in reason
    assert "owner" in reason


@pytest.mark.parametrize("action,params", [
    ("message", {"surface": "telegram", "target": OWNER_TG, "text": "no tranche"}),
    ("message", {"surface": "telegram", "target": f"telegram:{OWNER_TG}", "text": "t"}),
    ("message", {"target": "owner", "text": "no tranche"}),
    ("message", {"text": "no tranche"}),
    ("email_send", {"to": OWNER_EMAIL, "subject": "s", "body": "b"}),
    ("send_message", {"text": "no tranche; the guard refused"}),
    ("owner_ask", {"question": "pin PNL?"}),
    ("done", {"text": "no tranche"}),
    ("twitter_get_timeline", {"user": "tmachinroBot"}),
    ("defi_trade_swap", {"chain": "robinhood"}),
])
def test_tainted_run_owner_and_non_outbound_actions_pass(action, params):
    c = _controller()
    _taint(c)
    pre = gate.make_refusal_taint_gate_hook(c)
    assert run(pre(action, params, _ctx())) is None


def test_send_message_into_a_room_is_refused_when_tainted():
    c = _controller(chat_key="agent:rob:telegram:group:-1002125904710")
    _taint(c)
    pre = gate.make_refusal_taint_gate_hook(c)
    assert run(pre("send_message", {"text": "no tranche"}, _ctx()))


def test_untainted_run_may_post_publicly():
    c = _controller()
    pre = gate.make_refusal_taint_gate_hook(c)
    assert run(pre("twitter_post", {"text": "Bought 0.01 ETH of PNL, tx 0xabc"}, _ctx())) is None
    assert run(pre("message", {"surface": "telegram", "target": "@example_channel",
                               "text": "tranche done"}, _ctx())) is None


def test_taint_is_per_run_not_global():
    c = _controller()
    _taint(c, session_id="run-1")
    pre = gate.make_refusal_taint_gate_hook(c)
    assert run(pre("twitter_post", {"text": "x"}, _ctx("run-1")))
    assert run(pre("twitter_post", {"text": "x"}, _ctx("run-2"))) is None


def test_clear_reopens_the_public_rail():
    c = _controller()
    _taint(c)
    refusal_taint.clear("run-1")
    pre = gate.make_refusal_taint_gate_hook(c)
    assert run(pre("twitter_post", {"text": "x"}, _ctx())) is None


def test_public_refusal_is_recorded(monkeypatch):
    recorded = []
    import core.security.refusals as refusals
    monkeypatch.setattr(refusals, "record_refusal",
                        lambda reason, **kw: recorded.append((reason, kw)))
    c = _controller()
    _taint(c)
    pre = gate.make_refusal_taint_gate_hook(c)
    run(pre("twitter_post", {"text": "x"}, _ctx()))
    assert recorded and recorded[0][0] == "refusal_withheld"
    assert recorded[0][1]["tool"] == "twitter_post"


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------

def test_store_is_bounded():
    for i in range(refusal_taint.MAX_RUNS + 50):
        refusal_taint.mark(f"s{i}", action="defi_trade_swap")
    assert len(refusal_taint._TAINTED) <= refusal_taint.MAX_RUNS
    assert refusal_taint.is_tainted(f"s{refusal_taint.MAX_RUNS + 49}")


def test_blank_session_never_taints():
    refusal_taint.mark("", action="defi_trade_swap")
    assert not refusal_taint.is_tainted("")


def test_owner_turn_drain_clears_the_taint():
    from agents.task.agent.core.user_ingress import _update_forged_turn_marker
    refusal_taint.mark("sess-owner", action="defi_trade_swap")
    orch = types.SimpleNamespace(session_id="sess-owner", _forged_turn_kind=None,
                                 _clear_correspondent_taint=lambda: None)
    _update_forged_turn_marker(orch, [{"kind": "comment", "content": "hi"}])
    assert not refusal_taint.is_tainted("sess-owner")


def test_forged_turn_drain_keeps_the_taint():
    from agents.task.agent.core.user_ingress import _update_forged_turn_marker
    from agents.task.agent.core.self_wake import FORGED_TURN_KINDS
    refusal_taint.mark("sess-auto", action="defi_trade_swap")
    orch = types.SimpleNamespace(session_id="sess-auto", _forged_turn_kind=None,
                                 _clear_correspondent_taint=lambda: None)
    _update_forged_turn_marker(orch, [{"kind": next(iter(FORGED_TURN_KINDS))}])
    assert refusal_taint.is_tainted("sess-auto")
