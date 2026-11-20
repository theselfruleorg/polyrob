"""122 W1: the reason of an act — bound once, read by the guard, the notice and the record."""
import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional

from core import act_reason as ar


def test_the_bound_reason_is_one_clean_capped_line():
    tok = ar.set_act_why("  buy   the dip\nnow " + "x" * 400)
    try:
        why = ar.current_act_why()
        assert why.startswith("buy the dip now") and "\n" not in why
        assert len(why) == ar.WHY_MAX
    finally:
        ar.reset_act_why(tok)
    assert ar.current_act_why() == ""


def test_a_task_sees_the_reason_bound_before_it_started():
    async def main():
        tok = ar.set_act_why("hold the line")
        try:
            return await asyncio.create_task(asyncio.sleep(0, result=ar.current_act_why()))
        finally:
            ar.reset_act_why(tok)
    assert asyncio.run(main()) == "hold the line"


def test_an_autonomous_money_act_without_a_reason_refuses():
    ctx = SimpleNamespace(user_id="u")
    assert ar.autonomous_why_refusal("autonomous", ctx, "") == ar.WHY_REFUSAL
    assert ar.autonomous_why_refusal("autonomous", ctx, "rebalance") is None


def test_an_owner_act_and_the_signer_recheck_need_no_reason():
    ctx = SimpleNamespace(user_id="u")
    assert ar.autonomous_why_refusal("owner_direct", ctx, None) is None
    assert ar.autonomous_why_refusal("owner_approved", ctx, None) is None
    # The remote signer re-runs the guard with no context.
    assert ar.autonomous_why_refusal("autonomous", None, None) is None


def test_the_guard_rule_reads_the_bound_reason_when_none_is_passed():
    tok = ar.set_act_why("the job's rule")
    try:
        assert ar.autonomous_why_refusal("autonomous", SimpleNamespace()) is None
    finally:
        ar.reset_act_why(tok)


@dataclass(frozen=True)
class _Intent:
    chain: str
    why: Optional[str] = None


def test_the_intent_gets_the_bound_reason_and_keeps_its_own():
    tok = ar.set_act_why("bound")
    try:
        assert ar.with_bound_why(_Intent("base")).why == "bound"
        assert ar.with_bound_why(_Intent("base", why="own")).why == "own"
        odd = SimpleNamespace(chain="base")          # not a dataclass: unchanged
        assert ar.with_bound_why(odd) is odd
    finally:
        ar.reset_act_why(tok)
    assert ar.with_bound_why(_Intent("base")).why is None


def test_short_params_never_hold_a_body_or_a_secret():
    line = ar.short_params({"text": "a public post " * 20, "password": "hunter2",
                            "chain": "base", "amount_in": 5, "why": "x",
                            "to": "0x" + "a" * 100, "empty": None})
    assert "a public post" not in line and "hunter2" not in line
    assert '"text": "<280 chars>"' in line and '"password": "<redacted>"' in line
    assert '"chain": "base"' in line and '"amount_in": 5' in line
    assert "why" not in line and "empty" not in line
    assert len(line) <= ar.SHORT_PARAMS_MAX


def test_the_source_names_who_decided(monkeypatch):
    from core import autonomy_marker as am
    monkeypatch.setattr(am, "cron_job_for_session", lambda s: "j1" if s == "cron" else None)
    monkeypatch.setattr(am, "goal_for_session", lambda s: "g1" if s == "goal" else None)
    assert ar.act_source("s", autonomous=False) == "owner"
    assert ar.act_source("cron", autonomous=True) == "job j1"
    assert ar.act_source("goal", autonomous=True) == "goal g1"
    assert ar.act_source("x", autonomous=True) == "agent"
    assert ar.act_source("x", autonomous=True, metadata={"mandate": "m7"}) == "mandate m7"
    tok = ar.set_act_source("income rule r1")
    try:
        assert ar.act_source("cron", autonomous=True) == "income rule r1"
    finally:
        ar.reset_act_source(tok)
