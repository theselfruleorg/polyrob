"""Scoring must separate "wrong" from "I don't know".

Both capability incidents this month were the agent being CONFIDENTLY WRONG,
not ignorant: on 2026-09-10 it refused a Solana capability it had, and on
2026-09-12 it told the owner a deployed bridge rail was absent from its tool
catalog. A binary pass/fail scorer cannot see that class — it records a miss,
which is also what an honest "I checked and cannot determine this" records.

So the eval scores four outcomes and `wrong` DOMINATES: a confidently wrong
answer that happens to contain the right keywords is still wrong. An honest
unknown is a separate, better outcome than a miss — an agent that knows the
edge of its own knowledge is the thing being measured.
"""
import pytest

from scripts.eval.run_eval import classify


def _goal(**kw):
    base = {"acceptance_keywords": ["aave", "supply"],
            "disqualifying_keywords": ["no such tool", "does not exist"],
            "honest_unknown_keywords": ["cannot determine", "could not verify"]}
    base.update(kw)
    return base


def test_all_acceptance_keywords_present_is_correct():
    assert classify("Aave v3 supply APY is 4.2%", _goal(), status="done") == "correct"


def test_a_capability_denial_is_wrong_even_with_every_acceptance_keyword():
    """The exact 2026-09-12 shape: a fluent, on-topic, capability-denying answer."""
    blob = "Aave supply is possible in principle but that tool does not exist here"
    assert classify(blob, _goal(), status="done") == "wrong"


def test_an_honest_unknown_is_its_own_outcome():
    assert classify("I could not verify the current rate from a source I trust",
                    _goal(), status="done") == "unknown"


def test_an_honest_unknown_does_not_outrank_a_disqualifier():
    blob = "that tool does not exist, so I cannot determine the rate"
    assert classify(blob, _goal(), status="done") == "wrong"


def test_a_silent_miss_is_neither_correct_nor_honest():
    assert classify("Here is some unrelated prose.", _goal(), status="done") == "miss"


def test_an_unfinished_goal_is_never_scored_correct():
    assert classify("Aave supply 4.2%", _goal(), status="failed") == "miss"


def test_an_unfinished_goal_can_still_be_wrong():
    """A run that died AFTER denying a real capability still denied it."""
    assert classify("that tool does not exist", _goal(), status="failed") == "wrong"


def test_a_goal_without_the_new_keyword_lists_still_scores():
    """Back-compat: the pre-existing suite entries have acceptance keywords only."""
    legacy = {"acceptance_keywords": ["402", "settle"]}
    assert classify("the 402 flow settles on chain", legacy, status="done") == "correct"
    assert classify("nothing relevant", legacy, status="done") == "miss"


def test_matching_is_case_insensitive():
    assert classify("AAVE SUPPLY", _goal(), status="done") == "correct"


# ---------------------------------------------------------------------------
# The suite itself
# ---------------------------------------------------------------------------

def _suite():
    from scripts.eval.run_eval import SUITE
    return SUITE


def test_every_goal_has_the_keys_the_seeder_and_scorer_read():
    for g in _suite():
        for key in ("tag", "title", "body", "tools", "max_steps",
                    "acceptance_keywords"):
            assert key in g, f"{g.get('tag', g.get('title'))} is missing {key}"


def test_a_money_grant_is_declared_not_incidental():
    """A seeded goal's `payload.tools` is consumed VERBATIM by the dispatcher —
    `_seed` writes through `GoalBoard.create`, not `goal_create`, so the money
    filter that protects an agent-authored goal does not apply here. A
    measurement harness can therefore hand out a real money grant.

    That is allowed (the `money` goal needs `x402_pay` for `x402_wallet_status`,
    a read verb on the spend tool) but it must be DECLARED. The prose "Do NOT
    spend" in a goal body is not a gate; `spends: true` is at least a visible
    one, and it stops the next goal from acquiring a grant silently."""
    from core.tool_capabilities import ids_with
    money = ids_with("money")
    for g in _suite():
        granted = sorted(set(g["tools"]) & money)
        if granted:
            assert g.get("spends") is True, (
                f"{g['tag']} grants money tools {granted} without declaring "
                f"spends: true")
        else:
            assert not g.get("spends"), (
                f"{g['tag']} declares spends: true but grants no money tool")


def test_the_defi_goals_never_grant_a_money_tool():
    """The DeFi competence goals measure knowledge, not execution. They read and
    dry-run only — a scored eval must never be able to move funds."""
    from core.tool_capabilities import ids_with
    money = ids_with("money")
    for g in _suite():
        if not (g["tag"].startswith("defi") or g["tag"].startswith("capability")):
            continue
        offenders = sorted(set(g["tools"]) & money)
        assert offenders == [], f"{g['tag']} grants money tools: {offenders}"


def test_the_capability_goals_disqualify_a_denial():
    """The class that bit twice must be scoreable, not just describable."""
    tagged = {g["tag"]: g for g in _suite()}
    capability = [g for t, g in tagged.items() if t.startswith("capability")]
    assert capability, "the suite must measure capability self-knowledge"
    for g in capability:
        assert g.get("disqualifying_keywords"), (
            f"{g['tag']} cannot detect a confidently-wrong answer")


def test_tags_are_unique():
    tags = [g["tag"] for g in _suite()]
    assert len(tags) == len(set(tags))
