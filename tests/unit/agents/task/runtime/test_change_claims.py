"""Change claims vs the action ledger (prod 2026-10-27/28: "Jobs are fixed" with
no cronjob_edit; "the pinned skill is rewritten" with no skill_manage; "already
paid" with nothing sent). Fake-ledger style of ``test_edit_verify.py``."""
import asyncio
from types import SimpleNamespace

import pytest

from agents.task.runtime import change_claims as cc


class _Action:
    def __init__(self, name, params=None):
        self._d = {name: params or {}}

    def model_dump(self, exclude_unset=True):
        return dict(self._d)


class _Result:
    def __init__(self, error=None, content=None):
        self.error = error
        self.extracted_content = content
        self.metadata = {}


class _Step:
    def __init__(self, name, params=None, ok=True, content=None):
        self.model_output = SimpleNamespace(action=[_Action(name, params)])
        self.result = [_Result(None if ok else "boom", content)]


class _Agent:
    def __init__(self, steps, is_sub=False):
        self.history = SimpleNamespace(history=list(steps))
        self._is_sub_agent = is_sub


class _Orch:
    def __init__(self, steps):
        self.agents = {"main": _Agent(steps)}

    def add(self, *steps):
        self.agents["main"].history.history.extend(steps)


def _say(text):
    return _Step("send_message", {"text": text})


# ---------------------------------------------------------------------------
# claim detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,kind", [
    ("Jobs are fixed. The judge job (18e545399f90) now runs 29 Oct 18:00 UTC.", cc.JOB),
    ("I updated the announce job to the new window.", cc.JOB),
    ("I rescheduled the judge to Thursday.", cc.JOB),
    ("I cancelled the repost job.", cc.JOB),
    ("Done: the pinned skill is rewritten to v2 terms.", cc.SKILL),
    ("I wrote the new rule to owner.md.", cc.OWNER_DOC),
    ("I already judged and paid the round.", cc.MONEY),
    ("I sent 32 PNL to the winner.", cc.MONEY),
    ("I edited the judge job.", cc.JOB),
    ("I updated job 18e545399f90 to the new time.", cc.JOB),
    ("I rewrote the promo-bounty skill.", cc.SKILL),
    ("Updated the x-posting skill with the new rule.", cc.SKILL),
    ("We paid the two winners.", cc.MONEY),
])
def test_claims_detected(text, kind):
    assert kind in cc.claimed_changes([text])


@pytest.mark.parametrize("text", [
    "I will update the job tomorrow.",
    "Want me to cancel that job too?",
    "I did NOT send any money.",
    "No job was changed.",
    "The judge job is scheduled for 29 Oct 18:00 UTC.",   # a state, not an act
    "The repost job is cancelled.",                       # a state, not an act
    "The pool is paid in PNL.",                           # a rule, not a payment
    "I proposed six payout cards; nothing moves until you confirm.",
    "I created the job on 26 Oct.",                       # an earlier turn
    "I sent 3 posts to the channel.",                     # not money
    "Should I rewrite the skill?",
    "The paid-authors file has 4 rows.",
    # review 2026-10-29 M1 (probe-verified false positives): the verb must act
    # on the job/skill as its direct object; money needs I/we as the payer.
    "I saved the digest to workspace/digest.md for the daily job.",
    "I moved on to the next job",
    "I updated you on the bounty job status",
    "I applied the dca skill to today's buy",
    "I wrote the post following the x-posting skill",
    "I recorded the winners in the skill notes",
    "Done. The customer has now paid invoice 12.",
])
def test_non_claims_ignored(text):
    assert cc.claimed_changes([text]) == set()


# ---------------------------------------------------------------------------
# ledger backing
# ---------------------------------------------------------------------------

def test_claim_without_write_is_unbacked():
    orch = _Orch([_Step("cronjob_show", {"job_id": "a"}), _say("Jobs are fixed.")])
    assert cc.unbacked_claims(orch, {}) == [cc.JOB]


def test_claim_with_successful_write_is_backed():
    orch = _Orch([_Step("cronjob_edit", {"job_id": "a"}), _say("Jobs are fixed.")])
    assert cc.unbacked_claims(orch, {}) == []


def test_run_now_backs_a_job_claim():
    orch = _Orch([_Step("cronjob_run_now", {"job_id": "a"}), _say("Jobs are fixed.")])
    assert cc.unbacked_claims(orch, {}) == []


def test_failed_write_does_not_back():
    orch = _Orch([_Step("cronjob_edit", {"job_id": "a"}, ok=False), _say("Jobs are fixed.")])
    assert cc.unbacked_claims(orch, {}) == [cc.JOB]


def test_write_from_earlier_turn_does_not_back():
    orch = _Orch([_Step("cronjob_edit", {"job_id": "a"})])
    baseline = cc.ledger_baseline(orch)
    orch.add(_say("Jobs are fixed."))
    assert cc.unbacked_claims(orch, baseline) == [cc.JOB]


def test_claim_from_earlier_turn_is_not_rechecked():
    orch = _Orch([_say("Jobs are fixed.")])
    baseline = cc.ledger_baseline(orch)
    orch.add(_say("Here is the summary you asked for."))
    assert cc.unbacked_claims(orch, baseline) == []


def test_owner_doc_read_does_not_back_a_write_claim():
    orch = _Orch([_Step("owner_doc_manage", {"action": "read"}),
                  _say("I wrote the new rule to owner.md.")])
    assert cc.unbacked_claims(orch, {}) == [cc.OWNER_DOC]
    orch2 = _Orch([_Step("owner_doc_manage", {"action": "patch"}),
                   _say("I wrote the new rule to owner.md.")])
    assert cc.unbacked_claims(orch2, {}) == []


def test_skill_backed_by_skill_manage_or_skill_file_edit():
    claim = _say("Done: the pinned skill is rewritten to v2 terms.")
    assert cc.unbacked_claims(_Orch([_Step("skill_manage", {"action": "edit"}), claim]), {}) == []
    edit = _Step("coding_str_replace", {"file_path": "skills/user_x/lane/SKILL.md"})
    assert cc.unbacked_claims(_Orch([edit, claim]), {}) == []


def test_money_backed_only_by_a_moved_payment():
    claim = _say("I paid the three winners.")
    sent = _Step("defi_trade_transfer", {}, content="Sent 32 PNL. tx 0xabc confirmed")
    assert cc.unbacked_claims(_Orch([sent, claim]), {}) == []
    queued = _Step("defi_trade_transfer", {}, content="Queued for owner approval")
    assert cc.unbacked_claims(_Orch([queued, claim]), {}) == [cc.MONEY]
    card = _Step("propose_action", {"line": "/send 32 PNL"})
    assert cc.unbacked_claims(_Orch([card, claim]), {}) == [cc.MONEY]


def test_done_text_is_checked_too():
    orch = _Orch([_Step("done", {"text": "I updated the judge job's schedule."})])
    assert cc.unbacked_claims(orch, {}) == [cc.JOB]


def test_sub_agent_words_are_not_the_reply():
    orch = _Orch([])
    orch.agents["sub"] = _Agent([_say("Jobs are fixed.")], is_sub=True)
    assert cc.unbacked_claims(orch, {}) == []


def test_corrected_reply_after_the_nudge_replaces_the_claim():
    orch = _Orch([_say("Jobs are fixed."), _Step("done", {"text": "ok"})])
    nudge_at = cc.ledger_baseline(orch)
    orch.add(_say("Correction: the job is not changed yet."), _Step("done", {"text": "x"}))
    assert cc.unbacked_claims(orch, {}, words_since=nudge_at) == []


def test_no_new_words_after_the_nudge_keeps_the_gap():
    orch = _Orch([_say("Jobs are fixed."), _Step("done", {"text": "ok"})])
    nudge_at = cc.ledger_baseline(orch)
    orch.add(_Step("done", {"text": ""}))
    assert cc.unbacked_claims(orch, {}, words_since=nudge_at) == [cc.JOB]


def test_texts():
    assert "cronjob_edit" in cc.claim_gap_guidance([cc.JOB])
    assert "proposed card is not a payment" in cc.claim_gap_guidance([cc.MONEY])
    assert cc.not_done_line([cc.JOB, cc.SKILL]) == (
        "⚠️ Not done: no job edit was recorded this turn; "
        "no skill write was recorded this turn.")


def test_fail_open_on_a_broken_orchestrator():
    assert cc.unbacked_claims(object(), {}) == []


# ---------------------------------------------------------------------------
# the run-loop seam
# ---------------------------------------------------------------------------

class _MM:
    def __init__(self):
        self.injected = []

    def inject_runtime_guidance(self, text, **kw):
        self.injected.append(text)


class _Controller:
    def __init__(self):
        self.sent = []

    def create_action_model(self):
        return lambda **kw: kw

    async def multi_act(self, actions, execution_context=None):
        self.sent.extend(a["send_message"]["text"] for a in actions)
        return []


def _loop_agent(orch, session_id="s1"):
    return SimpleNamespace(orchestrator=orch, message_manager=_MM(), controller=_Controller(),
                           session_id=session_id, _is_sub_agent=False)


def test_seam_nudges_once_then_tells_a_bound_user(monkeypatch):
    orch = _Orch([])
    orch._message_router, orch._chat_session_key = object(), "agent:x:telegram:1"
    monkeypatch.setattr("core.surfaces.room_policy.is_public_session", lambda o: False)
    agent = _loop_agent(orch)
    cc.start_turn(agent)
    orch.add(_say("Jobs are fixed."), _Step("done", {"text": "ok"}))
    assert asyncio.run(cc.check_at_done(agent)) is True
    assert "cronjob_edit" in agent.message_manager.injected[0]
    orch.add(_Step("done", {"text": "ok"}))
    assert asyncio.run(cc.check_at_done(agent)) is False
    assert agent.controller.sent == ["⚠️ Not done: no job edit was recorded this turn."]


def test_seam_write_after_nudge_clears_the_gap(monkeypatch):
    orch = _Orch([])
    agent = _loop_agent(orch)
    cc.start_turn(agent)
    orch.add(_say("Jobs are fixed."), _Step("done", {"text": "ok"}))
    assert asyncio.run(cc.check_at_done(agent)) is True
    orch.add(_Step("cronjob_edit", {"job_id": "a"}), _Step("done", {"text": "ok"}))
    assert asyncio.run(cc.check_at_done(agent)) is False
    assert agent.controller.sent == []


def test_seam_appends_to_the_unbound_reply_record(monkeypatch):
    from core.surfaces import turn_reply
    orch = _Orch([])
    orch.session_id = "s-unbound"
    monkeypatch.setattr("core.surfaces.user_delivery.record_interactive_reply",
                        lambda *a, **k: None)
    agent = _loop_agent(orch, session_id="s-unbound")
    cc.start_turn(agent)
    turn_reply.mark_reply_published(orch, "Jobs are fixed.")
    orch.add(_say("Jobs are fixed."), _Step("done", {"text": "ok"}))
    asyncio.run(cc.check_at_done(agent))
    orch.add(_Step("done", {"text": "ok"}))
    asyncio.run(cc.check_at_done(agent))
    assert turn_reply.last_reply_text(orch) == (
        "Jobs are fixed.\n\n⚠️ Not done: no job edit was recorded this turn.")
    assert agent.controller.sent == []


def test_seam_skips_sub_agents_and_clean_turns():
    orch = _Orch([])
    agent = _loop_agent(orch)
    cc.start_turn(agent)
    orch.add(_say("Here is the list."), _Step("done", {"text": "ok"}))
    assert asyncio.run(cc.check_at_done(agent)) is False
    agent._is_sub_agent = True
    orch.add(_say("Jobs are fixed."))
    assert asyncio.run(cc.check_at_done(agent)) is False


# ---------------------------------------------------------------------------
# wired into the real run loop (the verify-before-done harness)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_loop_nudges_once_then_tells_the_user(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from tests.unit.agents.test_verify_before_done_wired import _build_run_loop_agent
    monkeypatch.delenv("VERIFY_BEFORE_DONE", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setattr(cc, "unbacked_claims", MagicMock(return_value=[cc.JOB]))
    tell = AsyncMock()
    monkeypatch.setattr(cc, "_tell_user", tell)

    agent, max_steps = _build_run_loop_agent(max_steps=10)
    await agent.run(max_steps=max_steps, _continue_session=True)

    assert agent.step.await_count == 2          # one corrective step, then the run ends
    calls = agent.message_manager.inject_runtime_guidance.call_args_list
    assert [c.kwargs["source"] for c in calls] == ["change_claims"]
    tell.assert_awaited_once()
    assert tell.await_args.args[2] == "⚠️ Not done: no job edit was recorded this turn."


@pytest.mark.asyncio
async def test_run_loop_clean_turn_ends_at_first_done(monkeypatch):
    from unittest.mock import MagicMock
    from tests.unit.agents.test_verify_before_done_wired import _build_run_loop_agent
    monkeypatch.delenv("VERIFY_BEFORE_DONE", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setattr(cc, "unbacked_claims", MagicMock(return_value=[]))

    agent, max_steps = _build_run_loop_agent(max_steps=10)
    await agent.run(max_steps=max_steps, _continue_session=True)

    assert agent.step.await_count == 1
    agent.message_manager.inject_runtime_guidance.assert_not_called()


@pytest.mark.asyncio
async def test_run_loop_last_step_tells_the_user_without_a_nudge(monkeypatch):
    """Review 2026-10-29 M2: a claim gap on the LAST step has no step left for a
    correction. No nudge, the not-done line at once, and the run keeps the DONE
    stop (no false step-limit stop, no exhaustion-summary call)."""
    from unittest.mock import AsyncMock, MagicMock
    from tests.unit.agents.test_verify_before_done_wired import _build_run_loop_agent
    monkeypatch.delenv("VERIFY_BEFORE_DONE", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setattr(cc, "unbacked_claims", MagicMock(return_value=[cc.JOB]))
    tell = AsyncMock()
    monkeypatch.setattr(cc, "_tell_user", tell)
    summary = AsyncMock()
    monkeypatch.setattr(
        "agents.task.agent.core.exhaustion_summary.run_exhaustion_summary", summary)

    agent, max_steps = _build_run_loop_agent(max_steps=1)
    await agent.run(max_steps=max_steps, _continue_session=True)

    assert agent.step.await_count == 1
    agent.message_manager.inject_runtime_guidance.assert_not_called()
    tell.assert_awaited_once()
    assert tell.await_args.args[2] == "⚠️ Not done: no job edit was recorded this turn."
    assert agent.state.stop_reason == "done"
    summary.assert_not_awaited()


@pytest.mark.parametrize("text", [
    "I sent the daily digest at 09:00 UTC.",
    "I sent you 2 PDF files.",
    "I paid attention to the logs.",
])
def test_a_time_a_file_count_or_paid_attention_is_not_money(text):
    from agents.task.runtime.change_claims import claimed_changes
    assert "money" not in claimed_changes([text])
