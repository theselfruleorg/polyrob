"""Why can't this run use this verb? One typed answer, so nobody has to guess.

2026-09-08, twice, live: the agent told the owner *"defi_trade is owner-locked,
and \\"Go\\" in chat doesn't unlock it"* while the goal's own payload carried
``defi_trade`` and the runtime had already logged ``Tiered spend lane ON:
[defi_trade_swap, …]``. The actual refusal was ``PolicyGate: amount $96.18
exceeds catastrophic ceiling $5.00`` — a wallet cap, nothing to do with grants.

The agent collapses FOUR states into one sentence, and only the first two are
about grants at all::

    not_in_toolset   the tool was never granted to this run
    tool_not_loaded  granted, but a feature flag kept it unregistered
    over_cap         granted and loaded, but the amount exceeds a ceiling
    needs_approval   granted, loaded, under the ceiling, awaiting the owner

``data/prompts/skills/treasury-trading/SKILL.md`` already says, in bold, "Do NOT
escalate 'grant defi_trade'. It is granted, standing, on the cycle above" —
added after the same loop had run ~50 times. It happened twice more anyway,
because the session that misdiagnosed was the owner's own Telegram chat, which
never loads that skill. **Capability truth has to be carried by the mechanism,
not remembered by the model.** This module is that mechanism.

Pure and dependency-light on purpose: every input is passed in, so it is
testable without a wallet, a container or a network, and callers stay free to
resolve the numbers however they already do.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from core.owner_remedy import BLOCKER_REMEDIES, Remedy

#: Reported in this order. The rule: report the blocker the owner must clear
#: FIRST, not merely the one checked first. Approving a spend that the per-tx
#: ceiling will refuse anyway wastes the owner's tap and teaches them the
#: approval queue lies.
_PRECEDENCE = ("wrong_turn_kind", "paused", "not_in_toolset", "tool_not_loaded",
               "over_cap", "needs_approval")


@dataclass(frozen=True)
class Verdict:
    """A capability answer that names itself, its numbers, and its remedy."""
    kind: str
    detail: str = ""
    tool_id: str = ""
    verb: str = ""
    amount_usd: Optional[float] = None
    limit_usd: Optional[float] = None

    @property
    def blocked(self) -> bool:
        return self.kind != "granted"

    @property
    def remedy(self) -> Remedy:
        return BLOCKER_REMEDIES.get(self.kind, Remedy())

    def owner_line(self) -> str:
        """One line for a human, naming only actions that exist.

        Never says "grant" unless the verdict is genuinely a grant problem —
        that word, wrongly used, is what sent the owner hunting twice.
        """
        if not self.blocked:
            return f"{self.tool_id}.{self.verb} is available."
        r = self.remedy
        remedy = (r.telegram or r.cli or r.owner_side or r.none_because
                  or "no owner action is available")
        head = self.detail or self.kind.replace("_", " ")
        return f"{head} → {remedy}"


def _turn_is_forged(ctx: Any) -> bool:
    """A forged / delegated / correspondent-tainted turn can never hold a money
    verb. This mirrors the existing gate rather than re-deciding it."""
    if getattr(ctx, "is_sub_agent", False):
        return True
    if getattr(ctx, "role", None) == "leaf":
        return True
    meta = getattr(ctx, "metadata", None) or {}
    return meta.get("turn_kind") in ("self_wake", "delegation_result")


def resolve_verdict(tool_id: str, verb: str, ctx: Any, *,
                    amount_usd: Optional[float] = None,
                    per_tx_cap_usd: Optional[float] = None,
                    autonomous_ceiling_usd: Optional[float] = None,
                    loaded_tools: Optional[Sequence[str]] = None,
                    gate_flag_off: Optional[str] = None,
                    paused_scope: Optional[str] = None) -> Verdict:
    """The single answer. Unknown inputs are simply not asserted on.

    ``per_tx_cap_usd`` is the catastrophic ceiling (a safety limit, never a
    budget); ``autonomous_ceiling_usd`` is the amount above which the owner must
    approve. They are different numbers with different remedies, which is
    exactly the distinction the agent kept losing.
    """
    found = {}

    if _turn_is_forged(ctx):
        found["wrong_turn_kind"] = "this turn can never hold a money verb"
    if paused_scope:
        found["paused"] = f"autonomy is paused ({paused_scope})"

    granted = list(getattr(ctx, "tools", None) or [])
    if tool_id not in granted:
        found["not_in_toolset"] = (
            f"{tool_id} is not in this run's toolset")
    elif loaded_tools is not None and tool_id not in loaded_tools:
        flag = f" ({gate_flag_off} is off)" if gate_flag_off else ""
        found["tool_not_loaded"] = (
            f"{tool_id} was granted but never loaded{flag}")

    if amount_usd is not None:
        if per_tx_cap_usd is not None and amount_usd > per_tx_cap_usd:
            found["over_cap"] = (
                f"${amount_usd:.2f} exceeds the per-transaction ceiling "
                f"${per_tx_cap_usd:.2f}")
        elif (autonomous_ceiling_usd is not None
                and amount_usd > autonomous_ceiling_usd):
            found["needs_approval"] = (
                f"${amount_usd:.2f} is above the autonomous ceiling "
                f"${autonomous_ceiling_usd:.2f}")

    for kind in _PRECEDENCE:
        if kind in found:
            return Verdict(kind=kind, detail=found[kind], tool_id=tool_id,
                           verb=verb, amount_usd=amount_usd,
                           limit_usd=(per_tx_cap_usd if kind == "over_cap"
                                      else autonomous_ceiling_usd))
    return Verdict(kind="granted", tool_id=tool_id, verb=verb,
                   amount_usd=amount_usd)


__all__ = ["Verdict", "resolve_verdict"]
