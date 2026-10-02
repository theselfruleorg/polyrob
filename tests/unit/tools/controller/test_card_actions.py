"""present_choice / propose_action: the agent's cards (core/surfaces/cards.py).

A choice returns the owner's tapped option into the live turn; a proposal has
no confirm — its one action runs the QUOTE on the owner's seat."""
import asyncio
import threading
import time

import pytest

from core.surfaces import cards


class _Registry:
    def __init__(self):
        self.actions = {}

    def action(self, description, param_model=None, **kw):
        def deco(fn):
            self.actions[fn.__name__] = (fn, param_model, description)
            return fn
        return deco


class _Controller:
    def __init__(self, sid="s-owner", public=False):
        self.registry = _Registry()
        self.container = type("C", (), {"get_service": staticmethod(lambda name: None)})()
        self.user_id = "rob"
        self.session_id = sid
        self.orchestrator = type("O", (), {"_public_session": public,
                                           "_forged_turn_kind": None})()


def _ctx(sid="s-owner", role="orchestrator", is_sub_agent=False):
    return type("Ctx", (), {"user_id": "rob", "role": role, "is_sub_agent": is_sub_agent,
                            "session_id": sid, "metadata": {}})()


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "rob")
    sent = []

    async def _deliver(container, user_id, text, **kw):
        sent.append((user_id, text, kw))
        return "sent"

    monkeypatch.setattr("core.surfaces.user_delivery.deliver_user_message", _deliver)
    return sent


def _actions(**kw):
    from tools.controller.card_actions import register_card_actions
    c = _Controller(**kw)
    register_card_actions(c)
    return c.registry.actions


def test_choice_returns_the_tapped_option_into_the_turn(_owner):
    fn, model, _ = _actions()["present_choice"]
    params = model(question="Which chain for the payout?", options=["Base", "Ethereum"],
                   wait_seconds=10)

    def _tap_soon():
        for _ in range(100):
            open_ = cards.store().open_cards("rob")
            if open_:
                cards.press(open_[0].card_id, "2", "rob")
                return
            time.sleep(0.05)

    t = threading.Thread(target=_tap_soon)
    t.start()
    res = asyncio.run(fn(params, execution_context=_ctx()))
    t.join()
    assert res.error is None and "The owner picked: Ethereum" in res.extracted_content
    (_uid, _text, kw), = _owner
    assert kw["source"] == "action_card" and kw["card_id"]


def test_choice_times_out_closed_without_an_answer(monkeypatch):
    fn, model, _ = _actions()["present_choice"]
    monkeypatch.setattr(cards, "wait_for_answer",
                        lambda cid, t, **k: cards.store().get(cid))
    res = asyncio.run(fn(model(question="Which one?", options=["A", "B"], wait_seconds=10),
                         execution_context=_ctx()))
    assert "No pick" in res.extracted_content
    assert cards.store().open_cards("rob") == []


def test_choice_uses_the_terminal_prompt_when_there_is_one(monkeypatch, _owner):
    fn, model, _ = _actions()["present_choice"]

    async def _reader(prompt):
        assert "1. Keep" in prompt
        return "2"

    monkeypatch.setattr("core.approval_input.get_approval_input", lambda: _reader)
    res = asyncio.run(fn(model(question="Keep or sell?", options=["Keep", "Sell"]),
                         execution_context=_ctx()))
    assert "The owner picked: Sell" in res.extracted_content
    assert _owner == []   # answered in the terminal, nothing pushed


@pytest.mark.parametrize("kw,ctx_kw,needle", [
    ({"public": True}, {}, "room"),
    ({}, {"role": "leaf"}, "leaf"),
    ({}, {"is_sub_agent": True}, "leaf"),
])
def test_both_tools_refuse_rooms_and_leaves(kw, ctx_kw, needle):
    acts = _actions(**kw)
    for name, args in (("present_choice", dict(question="Which one?", options=["A", "B"])),
                       ("propose_action", dict(command="/send 1 native to 0xabc on base"))):
        fn, model, _ = acts[name]
        res = asyncio.run(fn(model(**args), execution_context=_ctx(**ctx_kw)))
        assert res.error and needle in res.error


def test_choice_refused_in_an_autonomous_run():
    from agents.task.goals.autonomy_marker import mark_autonomous
    mark_autonomous("s-auto-1", None, cron_job_id="j1")
    fn, model, _ = _actions(sid="s-auto-1")["present_choice"]
    res = asyncio.run(fn(model(question="Which one?", options=["A", "B"]),
                         execution_context=_ctx(sid="s-auto-1")))
    assert res.error and "owner_ask" in res.error


def test_non_owner_tenant_is_refused(monkeypatch):
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "someone")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setattr("core.config_policy.local_mode_enabled", lambda: False)
    fn, model, _ = _actions()["propose_action"]
    res = asyncio.run(fn(model(command="/send 1 native to 0xabc on base"),
                         execution_context=_ctx()))
    assert res.error and "not the owner" in res.error


def test_proposal_is_a_card_with_no_confirm(_owner):
    fn, model, _ = _actions()["propose_action"]
    addr = "0x2FAa2566d98FC6eac6eD5F2DbA182Ffd2142f0e7"
    res = asyncio.run(fn(model(command=f"/send 0.5 native to {addr} on base",
                               why="rebalance to base"), execution_context=_ctx()))
    assert res.error is None and "NOTHING was sent" in res.extracted_content
    card, = cards.store().open_cards("rob")
    assert card.kind == cards.KIND_PROPOSAL and card.origin == cards.ORIGIN_AGENT
    assert not any(a.command.endswith("_ok") for a in cards.card_actions(card))
    assert cards.press(card.card_id, "re", "rob").run == f"/send 0.5 native to {addr} on base"


@pytest.mark.parametrize("command", [
    "/send 1 native to 0xabc on base go",   # never carries go
    "/status",                              # not a money verb
    "/trade buy everything",                # no quote → never carded
])
def test_proposal_refuses_what_a_card_cannot_carry(command):
    fn, model, _ = _actions()["propose_action"]
    res = asyncio.run(fn(model(command=command), execution_context=_ctx()))
    assert res.error and "propose_action" in res.error
