"""One typed answer to "why can't I use this verb?", so the agent stops guessing.

2026-09-08: the agent told the owner "defi_trade is owner-locked" while the
goal's own payload carried `defi_trade` and the runtime had logged the spend
lane armed. The real blocker was a wallet cap. It collapsed four different
states into one wrong sentence, and only two of them were even about grants:

    not_in_toolset   — the tool was never granted to this run
    tool_not_loaded  — granted, but a feature flag kept it unregistered
    over_cap         — granted and loaded, but the amount exceeds a ceiling
    needs_approval   — granted, loaded, under the ceiling, awaiting the owner

The skill already told the agent not to make this mistake, in bold, after the
same loop ran ~50 times. Prose did not hold. This is the mechanical version.
"""
import pytest

from core.capability_verdict import Verdict, resolve_verdict


class _Ctx:
    """Minimal execution context: owner turn, not forged, not a leaf."""
    def __init__(self, tools=("defi_trade",), **kw):
        self.tools = list(tools)
        self.is_sub_agent = kw.get("is_sub_agent", False)
        self.role = kw.get("role", "orchestrator")
        self.metadata = kw.get("metadata", {})


class TestTheFourStates:
    def test_a_tool_absent_from_the_toolset_is_not_in_toolset(self):
        v = resolve_verdict("defi_trade", "swap", _Ctx(tools=("filesystem",)))
        assert v.kind == "not_in_toolset"

    def test_a_granted_tool_over_the_cap_is_over_cap_not_a_grant_problem(self):
        """The exact 2026-09-08 shape: granted, and refused for money reasons."""
        v = resolve_verdict("defi_trade", "swap", _Ctx(),
                            amount_usd=96.18, per_tx_cap_usd=5.00)
        assert v.kind == "over_cap"
        assert "defi_trade" not in (v.remedy.owner_side or "").split()[:2], (
            "an over-cap refusal must not read as a request to grant the tool")

    def test_a_granted_tool_under_the_cap_but_over_autonomous_needs_approval(self):
        v = resolve_verdict("defi_trade", "swap", _Ctx(),
                            amount_usd=96.18, per_tx_cap_usd=120.0,
                            autonomous_ceiling_usd=5.0)
        assert v.kind == "needs_approval"
        assert "/approve" in v.remedy.telegram

    def test_a_fully_clear_call_is_granted(self):
        v = resolve_verdict("defi_trade", "swap", _Ctx(),
                            amount_usd=1.0, per_tx_cap_usd=120.0,
                            autonomous_ceiling_usd=5.0)
        assert v.kind == "granted"
        assert v.blocked is False


class TestOrdering:
    def test_the_cap_is_reported_before_approval(self):
        """Both are true at $96 with a $5 cap and a $5 autonomous ceiling. The
        owner can act on only one of them, and raising the cap comes first --
        approving something the cap will refuse anyway wastes the owner's tap."""
        v = resolve_verdict("defi_trade", "swap", _Ctx(),
                            amount_usd=96.18, per_tx_cap_usd=5.0,
                            autonomous_ceiling_usd=5.0)
        assert v.kind == "over_cap"

    def test_a_missing_grant_outranks_a_cap(self):
        """No point telling the owner to raise a ceiling for a verb this run
        could never call."""
        v = resolve_verdict("defi_trade", "swap", _Ctx(tools=()),
                            amount_usd=96.18, per_tx_cap_usd=5.0)
        assert v.kind == "not_in_toolset"


class TestEveryVerdictIsOwnerActionable:
    @pytest.mark.parametrize("kind", [
        "not_in_toolset", "tool_not_loaded", "over_cap",
        "needs_approval", "paused", "wrong_turn_kind",
    ])
    def test_each_blocked_kind_carries_a_remedy(self, kind):
        from core.owner_remedy import BLOCKER_REMEDIES
        r = BLOCKER_REMEDIES[kind]
        assert r.telegram or r.cli or r.owner_side or r.none_because

    def test_a_verdict_renders_one_owner_line(self):
        v = resolve_verdict("defi_trade", "swap", _Ctx(),
                            amount_usd=96.18, per_tx_cap_usd=5.00)
        line = v.owner_line()
        assert "$96.18" in line and "$5.00" in line
        from core.owner_remedy import unknown_owner_actions
        assert unknown_owner_actions(line) == [], (
            "a verdict may never name an action the owner cannot perform")


class TestTheLiveRegression:
    def test_the_2026_09_08_call_never_reports_a_grant_problem(self):
        """Guard against the exact recurrence: granted tool + wallet cap."""
        v = resolve_verdict("defi_trade", "solana_swap", _Ctx(),
                            amount_usd=96.18, per_tx_cap_usd=5.00)
        assert v.kind != "not_in_toolset"
        assert "grant" not in v.owner_line().lower(), (
            "this is the sentence that reached the owner twice; it must be "
            "structurally impossible now")
