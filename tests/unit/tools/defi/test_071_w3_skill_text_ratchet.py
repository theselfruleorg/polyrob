"""071 W3 ratchet: the money skills name real verbs, never ask for hand
arithmetic, and only call a money-capable chain an exit chain.

The $7-vs-$6,780 PNL report (2026-09-29) was a figure the model converted
itself. 071 T4–T6 found the skills still asked for it: FIFO lots and realized
P&L "by hand" while the rail book keeps average cost, and an exit lane listing
Optimism, which no swap can reach. These checks keep the text true.
"""
import re
from pathlib import Path

from tools.defi.data_tool import DefiDataTool
from tools.defi.trade_tool import DefiTradeTool

ROOT = Path(__file__).resolve().parents[4]
SKILLS = sorted((ROOT / "data" / "prompts" / "skills").glob("*/SKILL.md"))

#: The skills that handle money figures (positions, P&L, value, exits, sizing).
MONEY_SKILLS = ("position-journal", "exits", "sizing-and-risk", "treasury-trading",
                "post-trade-verify", "pre-trade-check", "trade-execution", "dca",
                "stable-cash", "crypto-trading-safety", "robinhood-chain",
                "defi-liquidity", "defi-bridge", "token-launch", "memecoin-scouting")

_NEGATION = re.compile(r"\b(never|not|no|don't|do not|without|instead of|stop)\b", re.I)
_HAND_MATH = (
    re.compile(r"\b(compute|calculate|convert|work out|derive)\b[^.]{0,80}"
               r"\b(by hand|yourself|in your head)\b", re.I),
    re.compile(r"\b(pnl|p&l|profit|loss|cost basis|realized)\b[^.]{0,40}\bby hand\b", re.I),
    re.compile(r"\bfirst-in,? first-out\b|\bFIFO\b", re.I),
)


def _skill_texts():
    for path in SKILLS:
        yield path.parent.name, path.read_text(encoding="utf-8")


def _sentences(text: str):
    flat = re.sub(r"\s+", " ", text)
    return re.split(r"(?<=[.!?])\s+|\s*\|\s*", flat)


def test_every_defi_verb_a_skill_names_is_a_real_action():
    missing = []
    for sid, text in _skill_texts():
        for tool, verb in re.findall(r"\bdefi_(data|trade)\.([a-z_0-9]+)", text):
            cls = DefiDataTool if tool == "data" else DefiTradeTool
            if not hasattr(cls, verb):
                missing.append(f"{sid}: defi_{tool}.{verb}")
    assert missing == [], "skills name verbs that do not exist:\n" + "\n".join(missing)


def test_no_money_skill_asks_for_hand_arithmetic():
    """A money figure the model repeats comes from a verb. A sentence that tells
    the model NOT to compute one is fine; one that tells it to is not."""
    bad = []
    for sid, text in _skill_texts():
        if sid not in MONEY_SKILLS:
            continue
        for sentence in _sentences(text):
            for pat in _HAND_MATH:
                if pat.search(sentence) and not _NEGATION.search(sentence):
                    bad.append(f"{sid}: {sentence.strip()[:140]}")
    assert bad == [], "hand arithmetic in a money skill:\n" + "\n".join(bad)


def test_the_book_skills_point_at_the_positions_verb():
    texts = dict(_skill_texts())
    for sid in ("position-journal", "exits", "sizing-and-risk"):
        assert "defi_data.positions" in texts[sid], sid
    assert "average cost" in texts["position-journal"].lower()


def test_someone_elses_wallet_is_wallet_holdings():
    texts = dict(_skill_texts())
    assert "defi_data.wallet_holdings" in texts["treasury-trading"]
    assert "defi_data.wallet_holdings" in texts["position-journal"]


def test_every_chain_a_skill_calls_an_exit_chain_can_move_money():
    """`exits` lists the chains the exit lane reaches. Each must be money-capable
    (Solana moves money on its own svm verbs, not the EVM ones)."""
    from core.wallet import chains
    text = dict(_skill_texts())["exits"]
    m = re.search(r"pinned USDC where the registry pins one \(([^)]*)\)", text)
    assert m, "the exit-lane chain list moved — update this ratchet"
    named = re.findall(r"\b([A-Z][a-z]+)\b", m.group(1))
    names = {"Base": "base", "Ethereum": "ethereum", "Arbitrum": "arbitrum",
             "Optimism": "optimism", "Polygon": "polygon", "Solana": "solana",
             "Robinhood": "robinhood"}
    for word in named:
        if word in ("USDC", "Chain"):
            continue
        chain = names.get(word)
        assert chain, f"unknown chain word {word!r} in the exit lane list"
        row = chains.get(chain)
        ok = chains.money_capable(chain)[0] or (row is not None and row.family == "svm")
        assert ok, f"exits names {chain} as an exit chain, but money_capable({chain!r}) is False"


def test_treasury_chain_table_says_which_chains_are_read_only():
    """Every chain in the treasury-trading table that the registry marks
    read-only is marked `no` in its Money column, and vice versa."""
    from core.wallet import chains
    text = dict(_skill_texts())["treasury-trading"]
    rows = re.findall(r"^\| \*\*(\w+)\*\* \| ([^|]+) \|", text, re.M)
    assert rows, "the chain table moved — update this ratchet"
    for chain, money in rows:
        row = chains.get(chain)
        assert row is not None, chain
        capable = chains.money_capable(chain)[0] or row.family == "svm"
        says_yes = money.strip().lower().lstrip("*").startswith("yes")
        assert says_yes == capable, f"{chain}: table says {money.strip()!r}"
