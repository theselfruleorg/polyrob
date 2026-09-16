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
        why="allowed_self_goal_tools() would need to start intersecting a spend "
            "verb (directly, or via a future AUTONOMOUS_MODE_TOOLS addition) for "
            "this to flip",
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
