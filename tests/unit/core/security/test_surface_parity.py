"""W5 (D3) surface parity: the same action, the same TurnFacts, the same decision.

The owner reaches a money or outward verb from five places — Telegram, the CLI,
the console, the HTTP API and cron. Each builds its execution context through
the one constructor (``core.security.turn_facts.owner_seat_context``) or the
agent's own step loop, and every policy decision reads that context through the
one owner-turn definition. So two contexts with the same TurnFacts must get the
same decision from every gate, whichever surface built them.
"""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

OWNER = "owner-1"
PAUSED = "paused (test)"


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", OWNER)
    monkeypatch.setenv("POLYROB_TOOL_DENYLIST", "")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("SECURITY_SETTINGS", raising=False)
    import core.autonomy_marker as marker
    monkeypatch.setattr(marker, "is_autonomous", lambda sid: str(sid or "").startswith("auto-"))
    from core.security import refusal_taint
    monkeypatch.setattr(refusal_taint, "is_tainted", lambda sid: False)
    # The owner's pause is ON: only a genuine owner turn passes it.
    from core.money import authority
    monkeypatch.setattr(authority, "spend_pause_refusal", lambda entry=False: PAUSED)
    import core.effects as effects
    monkeypatch.setattr(effects, "external_write_pause_gate_enabled", lambda: True)
    monkeypatch.setattr(effects, "effect_pause_refusal", lambda *a, **k: PAUSED)
    monkeypatch.setattr(effects, "classify_effect",
                        lambda tool, action, **k: effects.EffectVerdict("money", "action"))
    import cli.commands.wallet as wallet
    monkeypatch.setattr(wallet, "_admin_tenant", lambda user_id=None: user_id or OWNER)


def _decisions(ctx):
    from core.money.authorize import SpendIntent, authorize_spend
    from core.security import owner_turn
    from tools.controller.policy_chain import seat_pipeline
    from tools.controller.turn_origin import (
        _is_autonomous_goal_turn, _is_forged_or_autonomous_turn)
    probes = dict(forged_fn=_is_forged_or_autonomous_turn,
                  autonomous_ok_fn=_is_autonomous_goal_turn)
    pipe = seat_pipeline(tool_id="defi_trade", user_id=str(getattr(ctx, "user_id", "")))
    return {
        "verb_gate": authorize_spend(SpendIntent.verb_gate("bridge"), ctx).outcome,
        "turn": authorize_spend(SpendIntent.turn_origin(), ctx, **probes).outcome,
        "pause": authorize_spend(SpendIntent.pause_recheck(entry=True), ctx, **probes).reason,
        "owner_turn": owner_turn.owner_turn_refusal(ctx, verb="v", does="d", public="p"),
        "compute": owner_turn.compute_turn_allows(ctx),
        "genuine": owner_turn.is_genuine_owner_turn(ctx),
        "chain_pre": asyncio.run(pipe.run_pre("defi_trade_bridge", {}, ctx)),
    }


def _facts(ctx):
    from core.security.turn_facts import facts_of
    return replace(facts_of(ctx), seat="", turn_id="")


def _owner_seats():
    from cli.commands import wallet
    from core.security.turn_facts import owner_seat_context
    from core.wallet.token_trust import owner_seat_ctx
    from surfaces.telegram import send_ops, token_ops
    return {
        "telegram": token_ops._owner_ctx(OWNER),
        "telegram_send": send_ops._owner_ctx(OWNER),
        "cli": wallet._owner_ctx(),
        "console": owner_seat_ctx(OWNER, "console"),
        "api": owner_seat_context(OWNER, seat="api"),
    }


def test_every_owner_seat_has_the_same_facts():
    facts = {name: _facts(ctx) for name, ctx in _owner_seats().items()}
    first = next(iter(facts.values()))
    assert first.origin == "owner" and first.principal == OWNER
    assert all(f == first for f in facts.values()), facts


def test_every_owner_seat_gets_the_same_decision():
    decisions = {name: _decisions(ctx) for name, ctx in _owner_seats().items()}
    first = next(iter(decisions.values()))
    # The owner acting at a seat: principal + leaf pass, the pause does not bind
    # the owner, no chain stage refuses.
    assert first == {"verb_gate": "act", "turn": "act", "pause": None, "owner_turn": None,
                     "compute": True, "genuine": True, "chain_pre": None}
    assert all(d == first for d in decisions.values()), decisions


def test_the_owner_chat_turn_is_the_same_as_a_seat():
    """The agent's own step context of a genuine owner chat turn (Telegram DM,
    the REPL, the console chat) has the seat's facts and its decisions."""
    chat = SimpleNamespace(user_id=OWNER, role="orchestrator", is_sub_agent=False,
                           session_id="chat-1", metadata={"turn_kind": None})
    seat = _owner_seats()["api"]
    assert _facts(chat).origin == _facts(seat).origin == "owner"
    assert _decisions(chat) == _decisions(seat)


def test_cron_and_goal_runs_get_the_autonomous_decision():
    """A cron-dispatched run and a goal-dispatched run are the same autonomous
    origin; both get the same decision (the pause binds them)."""
    cron = SimpleNamespace(user_id=OWNER, role="orchestrator", is_sub_agent=False,
                           session_id="auto-cron-1", metadata={"turn_kind": None})
    goal = SimpleNamespace(user_id=OWNER, role="orchestrator", is_sub_agent=False,
                           session_id="auto-goal-7", metadata={"turn_kind": None})
    assert _facts(cron) == replace(_facts(goal), session_id=_facts(cron).session_id)
    d = _decisions(cron)
    assert d == _decisions(goal)
    assert d["pause"] == PAUSED and d["chain_pre"] == PAUSED and not d["genuine"]


def test_cron_delivery_is_a_leaf_like_a_sub_agent():
    from cron.delivery import _delivery_context
    delivery = _delivery_context(SimpleNamespace(session_id="auto-cron-1", user_id=OWNER))
    leaf = SimpleNamespace(user_id=OWNER, role="leaf", is_sub_agent=False,
                           session_id="auto-cron-1", metadata={"turn_kind": "cron"})
    assert _facts(delivery).origin == _facts(leaf).origin == "leaf"
    assert _decisions(delivery) == _decisions(leaf)
    assert _decisions(delivery)["verb_gate"] == "refused"


def test_a_stranger_at_any_seat_is_refused_the_same():
    from core.security.turn_facts import owner_seat_context
    decisions = [_decisions(owner_seat_context("stranger", seat=s))
                 for s in ("telegram", "cli", "console", "api")]
    assert all(d == decisions[0] for d in decisions)
    assert decisions[0]["verb_gate"] == "refused" and decisions[0]["genuine"] is False
