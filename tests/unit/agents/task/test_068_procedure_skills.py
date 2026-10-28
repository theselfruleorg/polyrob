"""068 W2: the basic trading procedure skills.

081 Q2: no skill loads by keyword any more (the 2026-09-25 wrong-token buy
started with a trading playbook pinned by a bare common word). A pin loads its
skill and the ``requires`` procedures it assumes; every other money skill is an
index line, shown only to a session that holds its money domain.
"""
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
    "token-identity": "verify the token contract address before buying TKN",
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

ALL_MONEY = set(TARGETS) | set(SPLIT) | {"treasury-trading", "crypto-trading-safety"}
VENUE = ["polymarket", "hyperliquid", "polymarket_data", "hyperliquid_data"]


def _pinned(pins, tools=MONEY, sm=None, **kw):
    sm = sm or SkillManager()
    return {m.skill_id for m in sm.get_pinned_skills(seeded_skill_ids=pins, tool_ids=tools, **kw)}


def _index(tools):
    return {m.skill_id for m in SkillManager().get_catalog_skills(tool_ids=tools, max_skills=500)}


def test_every_money_skill_is_an_index_line_for_a_money_session():
    ids = _index(MONEY + ["launchpad", "dapp_browser"] + VENUE)
    assert ALL_MONEY <= ids, ALL_MONEY - ids


@pytest.mark.parametrize("pin", ["treasury-trading", "dca", "memecoin-scouting"])
def test_a_pinned_doctrine_carries_identity_and_pre_trade(pin):
    """Codex A1: a trading doctrine without the procedures it assumes."""
    ids = _pinned([pin], MONEY + VENUE)
    assert {pin, "token-identity", "pre-trade-check"} <= ids, ids


def test_prerequisites_are_bounded_and_marked():
    got = SkillManager().get_pinned_skills(seeded_skill_ids=["treasury-trading"], tool_ids=MONEY)
    prereq = [m for m in got if m.trigger_type == "prerequisite"]
    assert len(prereq) <= SkillManager.MAX_PREREQUISITES
    assert all(m.match_reasons[0].startswith("requires:") for m in prereq)


def test_prerequisites_respect_their_own_tool_gate():
    sm = SkillManager()
    sm._ensure_rules_loaded()
    rules = dict(sm.skill_rules)
    rules["needs-money"] = {"auto_activate": True, "priority": 1, "requires": ["token-identity"],
                            "triggers": {}}
    sm.skill_rules = rules
    sm._load_skill_content = lambda sid, **kw: f"body of {sid}"
    ids = _pinned(["needs-money"], ["task"], sm=sm)
    assert "needs-money" in ids and "token-identity" not in ids
    ids = _pinned(["needs-money"], ["defi_data"], sm=sm)
    assert "token-identity" in ids


@pytest.mark.parametrize("sid", sorted(set(TARGETS) | set(SPLIT) | {"treasury-trading"}))
def test_skill_shape(sid):
    text = (SKILLS / sid / "SKILL.md").read_text(encoding="utf-8")
    assert re.search(rf"^name: {re.escape(sid)}$", text, re.M)
    desc = re.search(r"^description: '(.*)'$", text, re.M).group(1)
    assert len(desc) <= 1024
    from core.skills.frontmatter import strip_frontmatter
    from agents.task.agent.skill_manager import MAX_SKILL_FILE_CHARS
    assert len(strip_frontmatter(text)) < MAX_SKILL_FILE_CHARS
    if sid in TARGETS:
        assert len(text.splitlines()) < 200
        assert "## If a tool is missing" in text
    from core.skills.catalog import load_builtin_rules
    rule = load_builtin_rules(SKILLS)[sid]
    assert rule["auto_activate"] is True
    money = {"defi_trade", "defi_data", "x402_pay", "launchpad", "dapp_browser"}
    assert money & set(rule["triggers"]["tool_ids"]), "a money skill must be tool-gated"


def test_a_session_without_money_tools_sees_no_money_skill():
    assert not _index(["task", "web_fetch"]) & ALL_MONEY


# ---- Codex round 2 ----------------------------------------------------------

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
    ids = _pinned(["dca", "trade-execution"], sm=sm)
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
    ids = _pinned(["treasury-trading"], sm=sm, user_id="u1")
    assert "token-identity" not in ids and "pre-trade-check" in ids, ids
    ids = _pinned(["token-identity"], sm=sm, user_id="u1")
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


# ---- 068 round 3 (target wording) -------------------------------------------

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


# ---- 081 WS-4.4: every shipped body fits the pin budget ----------------------

def _bodies():
    from core.skills.frontmatter import strip_frontmatter
    root = Path(__file__).resolve().parents[4]
    for md in [*SKILLS.glob("*/SKILL.md"), *root.glob("packs/*/polyrob_*/skills/*/SKILL.md")]:
        yield md.parent.name, strip_frontmatter(md.read_text(encoding="utf-8"))


def test_every_shipped_body_fits_the_pin_budget():
    """Review A-1: treasury-trading's 25.5K body was never pinned, and its
    load_skill result was cut on the wire before the NO ROUTE rule."""
    from agents.task.agent.skill_manager import MAX_SKILL_INJECT_CHARS
    big = {sid: len(body) for sid, body in _bodies() if len(body) > MAX_SKILL_INJECT_CHARS}
    assert big == {}, f"move detail to references/: {big}"


def test_treasury_trading_states_no_route_in_its_head_and_keeps_its_references():
    body = dict(_bodies())["treasury-trading"]
    head = body[:body.index("## If a tool is missing")]
    assert "NO ROUTE is one token's fact, not a stop" in head
    assert "keep taking new screened entries" in head
    refs = SKILLS / "treasury-trading" / "references"
    for name in ("chains.md", "executing.md", "nfts.md", "learning-the-meta.md"):
        assert (refs / name).is_file(), name
        assert f"`{name}`" in head or f"references/{name}" in body, name

