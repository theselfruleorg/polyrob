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


def _err(text="refused: two contracts claim the PNL ticker", kind=None):
    return types.SimpleNamespace(error=text,
                                 metadata=({"error_kind": kind} if kind else None))


_PRE = refusal_taint.PRECONDITION


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


@pytest.mark.parametrize("verb", ["agent_nft_journal", "agent_nft_bind_identity"])
def test_permanent_chain_text_is_an_outbound_rail(verb):
    controller = _controller()
    _taint(controller)
    why = run(gate.make_refusal_taint_gate_hook(controller)(verb, {"text": "internal detail"}, _ctx()))
    assert why == refusal_taint.PUBLIC_REFUSAL_TEXT


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
    assert refusal_taint.is_tainted('s0')  # Cache eviction cannot release the run.


def test_taint_survives_process_state_reset_and_clear_is_durable():
    refusal_taint.mark('durable__delegate', action='defi_trade_swap')
    refusal_taint.reset_for_tests()
    assert refusal_taint.is_tainted('durable')
    refusal_taint.clear('durable')
    refusal_taint.reset_for_tests()
    assert not refusal_taint.is_tainted('durable')


def test_taint_survives_a_real_process_restart():
    import os
    import subprocess
    import sys
    from core.security import refusal_taint_store as store
    refusal_taint.mark('restart', action='defi_trade_swap')
    env = dict(os.environ, POLYROB_DATA_DIR=str(store.path().parent))
    result = subprocess.run([sys.executable, '-c',
        "from core.security.refusal_taint import is_tainted; assert is_tainted('restart')"],
        env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_unknown_taint_store_is_not_empty(monkeypatch):
    from core.security import refusal_taint_store as store
    store.path().parent.mkdir(parents=True, exist_ok=True)
    store.path().write_bytes(b'not a database')
    assert refusal_taint.is_tainted('unreadable')
    with pytest.raises(Exception):
        refusal_taint.clear('unreadable')


def test_failed_taint_write_holds_public_sends(monkeypatch):
    from core.security import refusal_taint_store as store
    monkeypatch.setattr(store, 'write', lambda *a, **kw: (_ for _ in ()).throw(OSError('disk full')))
    refusal_taint.mark('unsaved')
    refusal_taint._TAINTED.clear()
    assert refusal_taint.is_tainted('unsaved')


def test_reading_a_clean_run_does_not_create_a_store():
    from core.security import refusal_taint_store as store
    assert not refusal_taint.is_tainted('new-run')
    assert not store.path().exists()


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


# --------------------------------------------------------------------------
# a precondition / market error is not a refusal (prod 2026-10-08 08:06:54)
# --------------------------------------------------------------------------

_ALLOWANCE = ("insufficient allowance: the spender may pull 0 but this swap needs "
              "5000000. Call approve_token(token=0xabc, spender=0xdef, amount=5) "
              "first, then swap, then revoke_approval.")


@pytest.mark.parametrize("err", [
    _ALLOWANCE,
    "insufficient balance of 0xabc: 1 < 5",
    "cannot route USDC -> PNL: no pool with liquidity",
    "refused: the quote is stale (41s old, max 30s) — prices move; re-quote",
    "broadcast failed: execution reverted — nothing was sent",
    "rpc timeout",
])
def test_precondition_error_does_not_taint_and_the_notice_still_posts(err):
    """The owner's standing buyback: allowance error -> approve -> swap -> X notice."""
    c = _controller()
    post = gate.make_refusal_taint_record_hook(c)
    run(post("defi_trade_swap", {"chain": "base"}, _err(err, _PRE), _ctx()))
    assert not refusal_taint.is_tainted("run-1")
    why = run(gate.make_refusal_taint_gate_hook(c)(
        "twitter_post", {"text": "buyback done"}, _ctx()))
    assert why is None


@pytest.mark.parametrize("err", [
    "refused: two contracts claim the PNL ticker",
    "refused: this spend exceeds the daily cap (4000 USD)",
    "refused: route check DISAGREES — no route agrees with the oracle",
    "insufficient allowance and the spender is not on the allowlist",
    "broadcast outcome unknown: timed out. The transaction may have landed",
    "",
])
def test_a_true_refusal_still_taints(err):
    c = _controller()
    post = gate.make_refusal_taint_record_hook(c)
    run(post("defi_trade_swap", {"chain": "base"}, _err(err or "x"), _ctx()))
    if not err:
        assert refusal_taint.taints("")
        return
    assert refusal_taint.is_tainted("run-1")
    why = run(gate.make_refusal_taint_gate_hook(c)(
        "twitter_post", {"text": "buyback done"}, _ctx()))
    assert why == refusal_taint.PUBLIC_REFUSAL_TEXT


def test_a_veto_taints_even_with_precondition_words():
    c = _controller()
    gate.note_denied_action(c, "defi_trade_swap", _ctx(), "insufficient allowance")
    assert refusal_taint.is_tainted("run-1")


def test_prod_run_e0b00bd6_allowance_approve_swap_revoke_then_x_notice_posts(monkeypatch):
    """Prod 2026-10-07 16:03-16:06 (and 2026-10-08 d0ee1d13): the owner's buyback
    hit "insufficient allowance", approved, swapped OK, revoked, and its X
    announcement was withheld. The sequence must now post."""
    monkeypatch.setitem(_TOOLS, "approve_token", "defi_trade")
    monkeypatch.setitem(_TOOLS, "revoke_approval", "defi_trade")
    c = _controller()
    post = gate.make_refusal_taint_record_hook(c)
    ctx = _ctx("e0b00bd6")
    run(post("defi_trade_swap", {"chain": "base"}, _err(_ALLOWANCE, _PRE), ctx))
    run(post("approve_token", {}, _ok(), ctx))
    run(post("defi_trade_swap", {"chain": "base"}, _ok(), ctx))
    run(post("revoke_approval", {}, _ok(), ctx))
    assert not refusal_taint.is_tainted("e0b00bd6")
    assert run(gate.make_refusal_taint_gate_hook(c)(
        "twitter_post", {"text": "Buyback done: 0.1 ETH -> PNL"}, ctx)) is None


# --------------------------------------------------------------------------
# verifier round 3: the exemption is a TAG our code sets, never a phrase match
# --------------------------------------------------------------------------

@pytest.mark.parametrize("err", [
    "Refused: sell simulation reverted for token 0xabc (sell tax 99%) — not buying",
    "refused: the output mint does not match the declared target; simulation failed",
    "tx_guard: declared recipient 0xdead differs from calldata recipient — would revert",
    "refused: token 'timeout' (0xabc) failed the screen: proxy upgradeable",
    _ALLOWANCE,                       # the right words, but no tag: a refusal
])
def test_precondition_words_without_the_tag_still_taint(err):
    c = _controller()
    post = gate.make_refusal_taint_record_hook(c)
    run(post("defi_trade_swap", {"chain": "base"}, _err(err), _ctx()))
    assert refusal_taint.is_tainted("run-1")


def test_broadcast_error_kind_is_set_at_the_raise_site_only():
    from core.wallet.broadcast.evm import (BroadcastError, BroadcastOutcomeUnknown,
                                           broadcast_error_kind)
    assert broadcast_error_kind(BroadcastError("the node rejected the transaction "
                                               "(nonce too low): x", precondition=True)) == _PRE
    # a signer refusal, a wrong chain: same class, no tag
    assert broadcast_error_kind(BroadcastError("polyrob-signer refused: cap [x]")) is None
    assert broadcast_error_kind(BroadcastError("execution reverted")) is None
    # an outcome that may have landed is never a precondition
    assert broadcast_error_kind(BroadcastOutcomeUnknown("timed out")) is None
    assert broadcast_error_kind(RuntimeError("rpc timeout")) is None


def test_the_tool_result_helper_carries_the_kind():
    from tools.base_tool import BaseTool
    res = BaseTool._ar(None, error="cannot route A -> B: no pool", error_kind=_PRE)
    assert refusal_taint.error_kind(res) == _PRE
    assert refusal_taint.error_kind(BaseTool._ar(None, error="refused: cap")) is None


def test_store_write_failure_holds_only_that_run(monkeypatch):
    """Regression: one failed taint write held the public rails of EVERY run
    (owner chat, an owner's standing job) until a restart."""
    from core.security import refusal_taint_store

    def _boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(refusal_taint_store, "write", _boom)
    refusal_taint.mark("cronrun1", action="defi_trade_swap")
    assert refusal_taint.is_tainted("cronrun1") is True
    assert refusal_taint.is_tainted("ownerchat") is False
