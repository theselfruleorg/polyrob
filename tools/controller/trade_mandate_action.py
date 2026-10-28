"""``trade_mandate_propose`` — the agent puts the trading mandate's intake card to
the owner (086 W5, §3.1).

The ``trading-mandate`` skill runs the intake in chat (style, chains, allocation;
defaults stated; never re-ask). Then the agent calls this action ONCE with what
the owner said. It writes NO mandate and NO job: it validates the fields by the
mandate's own rules, stores a draft and posts ONE owner card whose echo line is
built from the validated draft and the preset (``core.trading_intake``) — never
from text the model typed. The owner's Confirm runs ``/trade start <ref>`` on the
owner's seat, which creates the mandate and its jobs with the owner's grant.

Controller-level, beside ``propose_action`` (``card_actions.py``), with the same
gates: refused in a room, for a leaf/sub-agent, in a session a correspondent
drives, and for a tenant that is not the owner. An autonomous run may propose it
too (the card waits in the owner's /cards); it starts nothing by itself.
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from agents.task.agent.views import ActionResult


def register_trade_mandate_action(controller) -> None:
    class TradeMandatePropose(BaseModel):
        budget_usd: float = Field(..., gt=0, description="The owner's allocation, USD.")
        chains: List[str] = Field(..., min_length=1, max_length=6,
                                  description="Networks to trade on, e.g. ['solana'].")
        preset: Literal["memecoin", "majors", "dca"] = Field(
            "memecoin", description="The style; its numbers come from the preset.")
        ticket_usd: Optional[float] = Field(None, gt=0,
                                            description="Only if the owner named one.")
        slots: Optional[int] = Field(None, ge=1, le=50,
                                     description="Max open; only if the owner named one.")

    @controller.registry.action(
        "After the trading-mandate intake: put the owner's trading mandate (allocation, "
        "networks, preset) to the owner as ONE card. Confirm starts it and all its jobs. "
        "Writes nothing itself.",
        param_model=TradeMandatePropose,
    )
    async def trade_mandate_propose(params: TradeMandatePropose,
                                    execution_context=None) -> ActionResult:
        from tools.controller.card_actions import _deliver, _refusal, _session_id
        from tools.controller.owner_ask_action import _user
        user_id = _user(controller, execution_context)
        why = _refusal(controller, execution_context, user_id)
        if why:
            return ActionResult(error=why, include_in_memory=True)
        from core.trading_mandate import MandateError, MandateUnreadable, get_active
        try:
            live = get_active(user_id)
        except MandateUnreadable as exc:
            return ActionResult(error=f"trade_mandate_propose: the trading store could not "
                                      f"be read ({exc}); no card was made.",
                                include_in_memory=True)
        if live is not None:
            return ActionResult(error=(
                f"trade_mandate_propose: trading mandate {live.id} is already "
                f"{live.status.upper()} (one at a time). The owner changes or ends it with "
                f"/trade pause|resume|stop; /trade status shows it. No card was made."),
                include_in_memory=True)
        fields = {"budget_usd": params.budget_usd, "chains": list(params.chains),
                  "preset": params.preset}
        if params.ticket_usd is not None:
            fields["ticket_usd"] = params.ticket_usd
        if params.slots is not None:
            fields["slots"] = params.slots
        sid = _session_id(controller, execution_context)
        from core import trading_intake
        try:
            card, m = trading_intake.intake_card(user_id, fields, origin="agent",
                                                 session_id=sid or None)
        except MandateError as exc:
            return ActionResult(error=f"trade_mandate_propose: {exc} — no card was made; "
                                      f"ask the owner for a valid value.",
                                include_in_memory=True)
        outcome = await _deliver(controller, user_id, card, sid)
        where = ("on the owner's chat" if outcome == "sent"
                 else "queued for the owner's chat (quiet hours)" if outcome == "queued"
                 else "in the owner's /cards list (no live chat reached)")
        return ActionResult(
            extracted_content=(f"Mandate card {card.card_id} is {where}. It reads: "
                               f"{trading_intake.echo_line(m)} NOTHING is started until the "
                               f"owner taps Confirm; do not say trading is running. After "
                               f"Confirm, code creates the hunt, the exit watches and the "
                               f"review — create no goal or job for it yourself."),
            include_in_memory=True)


__all__ = ["register_trade_mandate_action"]
