"""Generalized registry of "the code cannot do X" claims made in shipped prompts.

Owner directive 2026-08-27 18:50 UTC (via the /dev rail): "we need deeper
evaluation and ensure prompts are dynamic where required by system and that
agent is system capability aware even if we extend functionality."

Background: `test_shipped_prompts_match_capability.py` caught one instance of
this bug class — two prompts asserted "Solana has no signer" as permanent fact,
months after the Solana rail shipped and made that false. The agent then told
its own owner "screen-only by design" verbatim, every run, because the prompt
it read said so. That was a one-off, hand-written regression test anchored to
one capability.

This file is the reusable form. A capability-denial claim in a shipped prompt
is exactly as failure-prone as denial logic in code, and unlike code it is
never exercised by normal test runs — nothing calls a SKILL.md. The only way
to catch a claim going stale is to re-check it against the thing it claims
about, on every test run, forever. Register the claim once here; when the
underlying capability changes, this test fails at the exact row that broke,
naming the file, the phrase, and why it's now wrong — instead of the claim
silently rotting until an owner notices the agent refusing something it can
do (as happened with Solana).

Adding a NEW capability-denial claim to a skill or prompt? Add a matching
CapabilityClaim row here in the same commit (see docs/SKILL_AUTHORING_STANDARD.md
"Capability claims must be re-checkable, never asserted as permanent fact").
"""
import dataclasses
import pathlib
from typing import Callable

ROOT = pathlib.Path(__file__).resolve().parents[4]


@dataclasses.dataclass(frozen=True)
class CapabilityClaim:
    id: str
    file: str  # repo-relative path to the prompt/skill making the claim
    phrase: str  # case-insensitive substring anchoring the claim in the file
    still_true: Callable[[], bool]  # must return True while the denial is accurate
    why: str  # what would have to ship for this to flip to False


def _no_nft_marketplace_integration() -> bool:
    """True while nothing can BUY or SELL an NFT.

    ⚠️ Re-scoped 2026-09-15. This predicate used to fire on any method whose
    name contained "nft" and on any `tools/**/nft*.py` file, so the moment the
    hold/see/send/revoke verbs shipped it reported the claim false — even though
    the claim it guards ("no rail yet" to a PURCHASE) was still perfectly true.
    A guard that fires on the wrong capability teaches people to edit the guard.

    The claim is about a MARKETPLACE: buying, selling, listing, bidding. Those
    are what `defi_trade` still cannot do, and they are what this now checks.
    Holding, transferring and revoking are shipped and are not evidence of it.
    """
    import tools.defi.trade_tool as trade_tool
    methods = [n.lower() for n in dir(trade_tool.DefiTradeTool)
               if not n.startswith("_")]
    trade_words = ("buy", "sell", "list", "bid", "offer", "sweep")
    if any("nft" in m and any(w in m for w in trade_words) for m in methods):
        return False
    tools_dir = ROOT / "tools"
    for pattern in ("**/marketplace*.py", "**/seaport*.py", "**/reservoir*.py",
                    "**/opensea*.py", "**/blur*.py"):
        if any(tools_dir.glob(pattern)):
            return False
    return True


#: Verbs that move the treasury's money OUT (matches the codebase's own
#: income/SPEND terminology — see AGENTS.md's unified-ledger section). Deliberately
#: narrower than the "money" capability tag: x402_invoice is tagged "money" too
#: but only ever RECEIVES, so it's correctly allowed on a self-created goal —
#: the claim under test is about spending, not the tag.
_SPEND_TOOLS = frozenset({"defi_trade", "hyperliquid", "polymarket", "x402_pay"})


def _goal_create_still_excludes_money_tools() -> bool:
    """True while a self-created (agent-authored) goal can never be granted a
    money-SPEND tool, in ANY mode including full autonomy — the invariant the
    treasury skill tells the agent to rely on when explaining why it must wait
    for an owner-seeded goal rather than re-requesting the grant every run."""
    from tools.goal_tools import allowed_self_goal_tools
    return not (allowed_self_goal_tools() & _SPEND_TOOLS)



# ---- 068 W2 procedure skills ----------------------------------------------

def _trade_tool_methods():
    import tools.defi.trade_tool as trade_tool
    return [n.lower() for n in dir(trade_tool.DefiTradeTool) if not n.startswith("_")]


def _no_native_order_types_on_defi_trade() -> bool:
    """True while defi_trade has no limit/stop/trigger/DCA/TWAP order verb."""
    words = ("limit_order", "stop", "trigger", "order", "dca", "twap", "take_profit")
    return not any(w in m for m in _trade_tool_methods() for w in words)


def _no_hyperliquid_trigger_order() -> bool:
    """True while the Hyperliquid service exposes no TP/SL trigger-order verb.

    Only PUBLIC methods are verbs: the private ``_trigger_orders_in_scope`` /
    ``_trigger_cancel_refusal`` helpers guard a cancel that would REMOVE an
    owner's stop (c2a5ff0a8); they place nothing."""
    import packs.markets.polyrob_markets.hyperliquid.service as hl
    names = [n.lower() for c in vars(hl).values() if isinstance(c, type)
             and hasattr(c, "place_limit_order") for n in dir(c) if not n.startswith("_")]
    return not any(w in n for n in names for w in ("trigger", "tpsl", "take_profit", "stop_loss"))


def _no_price_impact_field() -> bool:
    """True while neither swap_quote nor swap reports a price-impact figure."""
    for rel in ("tools/defi/data_tool.py", "tools/defi/trade_tool.py"):
        text = (ROOT / rel).read_text(encoding="utf-8").lower()
        if "price_impact" in text or "price impact" in text:
            return False
    return True


def _no_onchain_strategy_budget_or_loss_breaker() -> bool:
    """True while the on-chain money path has no per-strategy budget or drawdown halt."""
    for base in ("core/money", "core/wallet", "tools/defi"):
        for path in (ROOT / base).rglob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            if any(w in text for w in ("strategy_budget", "per_strategy", "drawdown", "loss_breaker")):
                return False
    return True


def _no_lending_verb() -> bool:
    """True while defi_trade has no lend/supply/borrow verb (call can reach a protocol)."""
    return not any(w in m for m in _trade_tool_methods()
                   for w in ("lend", "supply", "borrow", "aave", "morpho"))


def _x402_payer_is_base_exact_only() -> bool:
    """True while the payer has no Solana and no `upto` path."""
    text = (ROOT / "tools/x402/real_client.py").read_text(encoding="utf-8").lower()
    return "upto" not in text and "solana_x402" not in text

CLAIMS = (
    CapabilityClaim(
        id="nft-no-rail",
        file="data/prompts/skills/treasury-trading/SKILL.md",
        phrase="no rail yet",
        still_true=_no_nft_marketplace_integration,
        why="a MARKETPLACE verb (Seaport, Reservoir, or similar buy/sell/list) "
            "would need to ship on defi_trade or a new tool for this claim to "
            "become false. The 2026-09-15 hold/see/send/revoke verbs do NOT "
            "make it false -- they are not a purchase rail",
    ),
    CapabilityClaim(
        id="goal-create-excludes-money-tools",
        file="data/prompts/skills/treasury-trading/SKILL.md",
        # Re-anchored 2026-09-08: efa589b0 rewrote this section ("defi_trade is a
        # standing grant on the cycle"), so the old "cannot trade" wording is gone
        # while the invariant below is unchanged. Anchor on the sentence that names
        # the mechanism `still_true` actually checks.
        phrase="strips the whole money set",
        still_true=_goal_create_still_excludes_money_tools,
        why="the UNARMED default: allowed_self_goal_tools() (no autonomy env) would "
            "need to start intersecting a spend verb for this to flip. Armed, it "
            "does include defi_trade by design (2026-09-12) -- the skill says so "
            "since Codex A8 (2026-09-26)",
    ),
    CapabilityClaim(
        id="068-no-native-dex-orders",
        file="data/prompts/skills/exits/SKILL.md",
        phrase="no native stop, limit or trigger orders",
        still_true=_no_native_order_types_on_defi_trade,
        why="a limit/stop/trigger/DCA/TWAP order verb would need to ship on defi_trade",
    ),
    CapabilityClaim(
        id="068-no-native-dca-order",
        file="data/prompts/skills/dca/SKILL.md",
        phrase="there is no native dca or",
        still_true=_no_native_order_types_on_defi_trade,
        why="a recurring/DCA order verb would need to ship on defi_trade",
    ),
    CapabilityClaim(
        id="068-no-hl-trigger-order",
        file="packs/markets/polyrob_markets/skills/hyperliquid-trading/SKILL.md",
        phrase="there is no trigger (tp/sl) order verb here",
        still_true=_no_hyperliquid_trigger_order,
        why="a Hyperliquid trigger (TP/SL) order method would need to ship",
    ),
    CapabilityClaim(
        id="068-exits-no-hl-trigger-order",
        file="data/prompts/skills/exits/SKILL.md",
        phrase="but no trigger (tp/sl) order",
        still_true=_no_hyperliquid_trigger_order,
        why="a Hyperliquid trigger (TP/SL) order method would need to ship",
    ),
    CapabilityClaim(
        id="068-no-price-impact-field",
        file="data/prompts/skills/pre-trade-check/SKILL.md",
        phrase="there is no price-impact field",
        still_true=_no_price_impact_field,
        why="swap_quote or swap would need to report a price-impact figure",
    ),
    # 068-no-realized-pnl RETIRED 2026-10-02 (071 W3): the rail book records
    # realized P&L per token (core/open_positions.py position_realized) and
    # defi_data.positions reads it; position-journal no longer makes the claim.
    CapabilityClaim(
        id="068-no-strategy-budget-or-breaker",
        file="data/prompts/skills/sizing-and-risk/SKILL.md",
        phrase="does not have yet: a per-strategy or per-goal budget",
        still_true=_no_onchain_strategy_budget_or_loss_breaker,
        why="a per-strategy budget or a drawdown/loss breaker would need to ship on the on-chain money path",
    ),
    CapabilityClaim(
        id="068-no-lending-verb",
        file="data/prompts/skills/stable-cash/SKILL.md",
        phrase="has no dedicated verb yet",
        still_true=_no_lending_verb,
        why="a lending/supply verb would need to ship on defi_trade",
    ),
    CapabilityClaim(
        id="068-x402-base-exact-only",
        file="data/prompts/skills/x402-pay/SKILL.md",
        phrase="not supported yet: solana payments, the `upto` scheme",
        still_true=_x402_payer_is_base_exact_only,
        why="the x402 payer would need a Solana (solana_x402) or `upto` path",
    ),
)


def test_every_registered_capability_claim_is_present_and_still_true():
    for claim in CLAIMS:
        path = ROOT / claim.file
        text = path.read_text(encoding="utf-8").lower()
        assert claim.phrase.lower() in text, (
            f"[{claim.id}] expected phrase {claim.phrase!r} not found in {claim.file} — "
            f"the claim was reworded or removed; update this registry entry to match "
            f"the new wording, or delete the row if the capability shipped and the "
            f"prompt was already updated")
        assert claim.still_true(), (
            f"[{claim.id}] {claim.file} still says {claim.phrase!r}, but this is now "
            f"FALSE: {claim.why}. The capability shipped — update the prompt to reflect "
            f"it (see the Solana incident this file exists to prevent), then remove or "
            f"rewrite this registry row.")
