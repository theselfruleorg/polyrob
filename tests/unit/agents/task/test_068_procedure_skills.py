"""068 W2: the basic trading procedure skills.

Each loads for the task it teaches and NOT for a routine task, because the
2026-09-25 wrong-token buy started with a trading playbook pinned into a
session by a bare common word. Every matching test runs at the PRODUCTION
``max_skills`` (the default), not a widened one (Codex A1, 2026-09-26).
"""
import json
import re
from pathlib import Path

import pytest

from agents.task.agent.skill_manager import SkillManager

SKILLS = Path(__file__).resolve().parents[4] / "data" / "prompts" / "skills"
MONEY = ["defi_trade", "defi_data", "x402_pay", "cronjob", "task"]

SPLIT = {
    "memecoin-scouting": "scan for fresh memecoin pairs and check the top holders",
    "robinhood-chain": "should I hunt on robinhood chain today",
    "defi-bridge": "bridge ETH from base to robinhood",
    "token-launch": "launch a token on the launchpad with a creator tax",
    "defi-liquidity": "add liquidity to the uniswap v3 pool",
}

TARGETS = {
    "token-identity": "verify the token contract address before buying PNL",
    "pre-trade-check": "check the price impact and slippage before a trade",
    "trade-execution": "execute the swap with a dry run first",
    "post-trade-verify": "verify the fill price and the tx receipt of that swap",
    "position-journal": "update the position ledger with the cost basis of each trade",
    "exits": "set a stop loss and a take profit for the new position",
    "sizing-and-risk": "decide the position size and risk per trade",
    "stable-cash": "check the stablecoin for a depeg",
    "x402-pay": "pay for the api with x402",
    "dca": "set up a dca into ETH every week",
}

ROUTINE = [
    "rotate the API token and update the ledger",
    "update my photography portfolio",
    "reconcile the inventory ledger",
    "add a dry_run flag to the database migration",
    "move the files to the base directory",
    "monitor the job market for data engineers",
    "buy a rug for the office",
    "resize the position of the button",
    "write the business exit plan",
    "write a blog post about our roadmap",
    "fix the failing unit test and add a target to the Makefile",
    "build the payload for the api client",
    "run the test suite and send the report",
    "claim the domain and check its position",
]


ALL_MONEY = set(TARGETS) | set(SPLIT) | {"treasury-trading", "crypto-trading-safety"}
VENUE = ["polymarket", "hyperliquid", "polymarket_data", "hyperliquid_data"]


def _ids(task, tools=MONEY):
    return {m.skill_id for m in SkillManager().get_skills_for_session(task=task, tool_ids=tools)}


@pytest.mark.parametrize("sid,task", sorted({**TARGETS, **SPLIT}.items()))
def test_each_skill_loads_for_its_task(sid, task):
    ids = _ids(task, MONEY + ["launchpad", "dapp_browser"])
    assert sid in ids, f"{sid!r} did not load for {task!r}: {ids}"


@pytest.mark.parametrize("task", ROUTINE)
def test_no_money_skill_loads_for_a_routine_task(task):
    ids = _ids(task, MONEY + ["launchpad", "dapp_browser"] + VENUE)
    assert not ids & ALL_MONEY, f"{task!r} loaded {ids & ALL_MONEY}"


@pytest.mark.parametrize("task", ["buy PNL", "swap USDC for ETH", "buy ETH every week",
                                  "sell the memecoin", "ape into a fresh memecoin"])
def test_a_generic_trade_loads_identity_and_pre_trade(task):
    """Codex A1: at the production cap a generic trade request loaded the
    playbook but not the procedures it assumes."""
    ids = _ids(task, MONEY + VENUE)
    assert {"token-identity", "pre-trade-check"} <= ids, f"{task!r} -> {ids}"


def test_prerequisites_are_bounded_and_marked():
    got = SkillManager().get_skills_for_session(
        task="execute the swap and grow the treasury", tool_ids=MONEY)
    prereq = [m for m in got if m.trigger_type == "prerequisite"]
    assert len(prereq) <= SkillManager.MAX_PREREQUISITES
    assert all(m.match_reasons[0].startswith("requires:") for m in prereq)


def test_prerequisites_respect_their_own_tool_gate():
    sm = SkillManager()
    sm._ensure_rules_loaded()
    rules = dict(sm.skill_rules)
    rules["needs-money"] = {"auto_activate": True, "priority": 1, "requires": ["token-identity"],
                            "triggers": {"keywords": ["zebra"], "tool_ids": []}}
    sm.skill_rules = rules
    sm._load_skill_content = lambda sid, **kw: f"body of {sid}"
    ids = {m.skill_id for m in sm.get_skills_for_session(task="zebra", tool_ids=["task"])}
    assert "needs-money" in ids and "token-identity" not in ids
    ids = {m.skill_id for m in sm.get_skills_for_session(task="zebra", tool_ids=["defi_data"])}
    assert "token-identity" in ids


@pytest.mark.parametrize("sid", sorted(set(TARGETS) | set(SPLIT) | {"treasury-trading"}))
def test_skill_shape(sid):
    text = (SKILLS / sid / "SKILL.md").read_text(encoding="utf-8")
    assert re.search(rf"^name: {re.escape(sid)}$", text, re.M)
    desc = re.search(r"^description: '(.*)'$", text, re.M).group(1)
    assert len(desc) <= 1024
    from agents.task.agent.skill_manager import MAX_SKILL_FILE_CHARS, parse_skill_frontmatter
    assert len(parse_skill_frontmatter(text)[1]) < MAX_SKILL_FILE_CHARS
    if sid in TARGETS:
        assert len(text.splitlines()) < 200
        assert "## If a tool is missing" in text
    rule = json.loads((SKILLS / "rules.json").read_text())[sid]
    assert rule["auto_activate"] is True
    money = {"defi_trade", "defi_data", "x402_pay", "launchpad", "dapp_browser"}
    assert money & set(rule["triggers"]["tool_ids"]), "a money skill must be tool-gated"


def test_a_session_without_money_tools_gets_none():
    ids = _ids("execute the swap, set a stop loss and buy ETH every week", ["task", "web_fetch"])
    assert not ids & ALL_MONEY


# ---- Codex round 2 ----------------------------------------------------------

@pytest.mark.parametrize("task,sid", [("Buy ETH every week", "dca"),
                                      ("buy ETH every week with a recurring buy", "dca")])
def test_the_requested_strategy_is_not_cut_by_generic_doctrine(task, sid):
    """N4: "Buy ETH every week" loaded the memecoin doctrine + token-identity at the
    production cap and cut `dca`, the skill the owner asked for."""
    ids = _ids(task, MONEY + VENUE)
    assert sid in ids and {"token-identity", "pre-trade-check"} <= ids, ids


def _manager_with(rules_patch, user_rules=None):
    sm = SkillManager()
    sm._ensure_rules_loaded()
    rules = dict(sm.skill_rules)
    rules.update(rules_patch)
    sm.skill_rules = rules
    sm._load_skill_content = lambda sid, **kw: f"body of {sid}"
    sm._load_user_rules = lambda uid: (user_rules or {}, None)
    return sm


def test_seeded_skills_pull_their_prerequisites():
    """N5: expansion ran BEFORE seeds, so a seeded `dca` came without its prerequisites."""
    sm = _manager_with({})
    got = sm.get_skills_for_session(task="hello there", tool_ids=MONEY,
                                    seeded_skill_ids=["dca", "trade-execution"])
    ids = {m.skill_id for m in got}
    assert {"dca", "trade-execution", "token-identity", "pre-trade-check"} <= ids, ids


def test_a_user_disabled_prerequisite_stays_disabled():
    """N5: a user rule that disables a skill is not overridden by another rule's
    `requires`; a skill the user seeds explicitly still loads."""
    import copy
    sm0 = SkillManager()
    sm0._ensure_rules_loaded()
    off = copy.deepcopy(sm0.skill_rules["token-identity"])
    off["auto_activate"] = False
    sm = _manager_with({}, user_rules={"token-identity": off})
    ids = {m.skill_id for m in sm.get_skills_for_session(
        task="buy PNL", tool_ids=MONEY, user_id="u1")}
    assert "token-identity" not in ids and "pre-trade-check" in ids, ids
    ids = {m.skill_id for m in sm.get_skills_for_session(
        task="hello", tool_ids=MONEY, user_id="u1", seeded_skill_ids=["token-identity"])}
    assert "token-identity" in ids


@pytest.mark.parametrize("path", [
    "data/prompts/skills/treasury-trading/SKILL.md",
    "data/prompts/skills/crypto-trading-safety/SKILL.md",
    "packs/markets/polyrob_markets/skills/hyperliquid-trading/SKILL.md",
    "packs/markets/polyrob_markets/skills/polymarket-trading/SKILL.md",
])
def test_doctrine_and_venue_skills_have_missing_tool_fallbacks(path):
    root = Path(__file__).resolve().parents[4]
    assert "## If a tool is missing" in (root / path).read_text(encoding="utf-8")


def test_no_generally_loaded_skill_asserts_a_standing_mandate():
    """N4: the owner's standing authority is a posture fact, not a skill fact."""
    for sid in ("treasury-trading", "crypto-trading-safety"):
        text = (SKILLS / sid / "SKILL.md").read_text(encoding="utf-8")
        assert "has granted a standing authority" not in text.lower()
        assert "has granted a standing" not in text.lower()


# ---- 068 round 3 (Codex R3-5, R3-6, target wording) -------------------------

def test_a_cut_parent_does_not_evict_its_prerequisite():
    """R3-5: identity (p1) is required only by doctrine (p4), which the cap cuts;
    bridge (p2) and liquidity (p3) require nothing. Identity must stay."""
    def rule(priority, kw, requires=()):
        return {"priority": priority, "auto_activate": True, "requires": list(requires),
                "triggers": {"keywords": [kw], "tool_ids": ["defi_trade"],
                             "task_patterns": [], "action_names": []}}
    sm = SkillManager()
    sm.skill_rules = {"identity": rule(1, "alpha"), "bridge": rule(2, "alpha"),
                      "liquidity": rule(3, "alpha"),
                      "doctrine": rule(4, "alpha", requires=["identity"])}
    sm._ensure_rules_loaded = lambda: None
    sm._load_skill_content = lambda sid, **kw: f"body of {sid}"
    sm._load_user_rules = lambda uid: ({}, None)
    ids = [m.skill_id for m in sm.get_skills_for_session(
        task="alpha", tool_ids=["defi_trade"], max_skills=2)]
    assert "identity" in ids and "doctrine" not in ids, ids


def test_a_selected_parent_still_carries_its_prerequisite():
    ids = _ids("buy ETH every week")
    assert {"dca", "token-identity", "pre-trade-check"} <= ids, ids


def test_no_skill_asserts_an_unconditional_grant():
    banned = ("already have the trade grant", "granted, standing",
              "its standing grant", "treasury: manage open positions")
    for path in list(SKILLS.glob("*/SKILL.md")):
        text = path.read_text(encoding="utf-8").lower()
        for phrase in banned:
            assert phrase not in text, (path.parent.name, phrase)


def test_no_skill_says_canonical_conversion_is_refused_under_a_target():
    for path in list(SKILLS.glob("*/SKILL.md")):
        text = path.read_text(encoding="utf-8")
        assert "USDC → WETH is a buy" not in text and "is a buy, and is refused" not in text, \
            path.parent.name
    ti = (SKILLS / "token-identity" / "SKILL.md").read_text(encoding="utf-8")
    assert "Canonical assets pass freely" in ti and "outside the target" in ti


def test_a_non_converging_dependency_graph_keeps_priority_order(monkeypatch):
    """R4-3 (Codex): A–F by priority, cap 2, D→[A,C], E→[A,B], F→[D] oscillates;
    the fallback is plain priority order, so priority-1 A is never lost."""
    from agents.task.agent.skill_manager import SkillManager
    sm = SkillManager()
    rules = {}
    for i, sid in enumerate("ABCDEF", start=1):
        rules[f"s{sid}"] = {"priority": i, "auto_activate": True,
                            "triggers": {"keywords": ["zzqx"], "tool_ids": [],
                                         "task_patterns": [], "action_names": []}}
    rules["sD"]["requires"] = ["sA", "sC"]
    rules["sE"]["requires"] = ["sA", "sB"]
    rules["sF"]["requires"] = ["sD"]
    sm.skill_rules = rules
    monkeypatch.setattr(sm, "_ensure_rules_loaded", lambda: None)
    monkeypatch.setattr(sm, "_load_skill_content", lambda sid, **kw: f"body {sid}")
    monkeypatch.setattr(sm, "_load_user_rules", lambda uid: ({}, None))
    ids = [m.skill_id for m in sm.get_skills_for_session(task="zzqx", tool_ids=[],
                                                         max_skills=2)]
    assert "sA" in ids
