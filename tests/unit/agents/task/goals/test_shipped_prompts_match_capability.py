"""The shipped prompts must not deny a capability the code actually has.

The agent on prod read only chain='base', reported no Solana balance for weeks,
and told the owner "Solana: screen-only by design ... no Solana signer/rail" —
verbatim, in its own run reports. That was not a model failure. Both prompts it
reads said so as fact:

    data/streams/streams.yaml:   "SOLANA has no signer at all"
    treasury-trading/SKILL.md:   "there is no Solana signer or rail, so you
                                  cannot buy"

Both were true when written and both were falsified when the Solana rail
shipped (signer, simulation, broadcast rail and defi_trade.solana_swap). A
prompt that denies a shipped capability is as much a defect as code that denies
it, and it is harder to notice, because everything downstream looks like the
agent choosing not to act. These tests fail the build instead.
"""
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[5]
MANIFEST = ROOT / "data" / "streams" / "streams.yaml"
SKILL = ROOT / "data" / "prompts" / "skills" / "treasury-trading" / "SKILL.md"

#: Claims that were true pre-rail and are false now. Matched case-insensitively
#: against the whole prompt text.
_FALSIFIED = (
    "no signer at all",
    "there is no solana signer",
    "no solana signer",
    "screen-only by design",
)


def _prompt_texts():
    return {"streams.yaml": MANIFEST.read_text(encoding="utf-8"),
            "SKILL.md": SKILL.read_text(encoding="utf-8")}


def test_the_solana_rail_actually_exists():
    """Anchors the tests below: if the rail is ever REMOVED, this fails first
    and the prompt assertions become wrong rather than silently inverted."""
    from tools.defi.trade_tool import DefiTradeTool
    assert hasattr(DefiTradeTool, "solana_swap")
    assert (ROOT / "core" / "wallet" / "solana_signer.py").is_file()
    assert (ROOT / "core" / "wallet" / "solana_rail.py").is_file()


def test_no_shipped_prompt_claims_solana_has_no_signer():
    for name, text in _prompt_texts().items():
        low = text.lower()
        for claim in _FALSIFIED:
            assert claim not in low, (
                f"{name} still claims {claim!r}; the Solana rail shipped, so "
                f"this tells the agent to refuse a capability it has")


def test_the_trading_stream_reads_the_solana_balance_every_run():
    """The owner's actual complaint was 'he never informs about solana balance
    and positions'. The manifest hardcoded portfolio(chain='base'), so the
    Solana balance was never read, let alone reported."""
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    stream = next(s for s in data["streams"] if s["id"] == "treasury-trading")
    bodies = " ".join(g["body"] for g in stream["goals"]).lower()
    assert "chain=''solana''" in bodies or "chain='solana'" in bodies, \
        "no goal reads the Solana portfolio, so its balance is never reported"


def test_the_trading_skill_marks_solana_as_money_capable():
    text = SKILL.read_text(encoding="utf-8")
    row = next(l for l in text.splitlines()
               if l.startswith("| **solana**"))
    assert "solana_swap" in row, \
        "the chain table must name the verb that moves value on Solana"
    assert "SOL" in row, "the row must say which asset pays Solana gas"


def test_the_skill_activates_on_a_solana_swap():
    """polyrob-triggers gates auto-activation. defi_trade_solana_swap was
    missing, so the one skill that governs trading did not fire on the Solana
    money verb."""
    text = SKILL.read_text(encoding="utf-8")
    assert "defi_trade_solana_swap" in text
