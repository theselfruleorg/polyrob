"""099 W1/W5: the agent's side of selling — ``offer_manage`` and ``order_deliver``.

``offer_manage`` (owner turns only, the same gates as ``propose_action``): draft
or edit an offer manifest, record its resources and a test, see offers and
orders, pause an offer, and put the two owner decisions on cards — the setup
budget (``/offer setup``) and go-live (``/offer live <id> <hash>``). It grants
nothing itself: every card's Confirm runs the owner's seat line, and every card
body is built from the VALIDATED manifest and the store, never from model text.

``order_deliver``: the fulfillment run (``metadata["mandate"]`` = that order)
records the proof, writes the receipt and tells the buyer — or reports a failure
(a retry, then the owner's refund). An owner turn may deliver by hand. A room,
a correspondent session or any other run is refused.
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from agents.task.agent.views import ActionResult

OPS = ("draft", "resource", "test", "propose_setup", "propose_live", "pause", "status",
       "orders")


def register_offer_actions(controller) -> None:
    class OfferManage(BaseModel):
        op: str = Field(..., description="One of: " + ", ".join(OPS))
        offer_id: Optional[str] = Field(None, description="The offer id (all ops but draft/status).")
        manifest: Optional[Dict[str, Any]] = Field(
            None, description="draft: the whole polyrob.offer/1 manifest (see the sell-anything skill).")
        kind: Optional[str] = Field(None, description="resource: contract|file_set|account|endpoint|storage")
        chain: Optional[str] = Field(None, description="resource: the chain of a contract")
        address: Optional[str] = Field(None, description="resource: the contract address")
        data: Optional[Dict[str, Any]] = Field(
            None, description="resource: details. A contract needs runtime_hash and functions "
                              "(the ONLY signatures a fulfillment may call, e.g. "
                              "[\"mint(address,string)\"]). test: the evidence.")
        budget_usd: Optional[float] = Field(None, ge=0, le=500,
                                            description="propose_setup: the on-chain setup budget")

    @controller.registry.action(
        "Sell something: draft/edit an offer, record its resources and a test, see offers and "
        "orders, pause one, and put the setup budget or go-live to the owner as ONE card each. "
        "Grants nothing itself.",
        param_model=OfferManage,
    )
    async def offer_manage(params: OfferManage, execution_context=None) -> ActionResult:
        from tools.controller.card_actions import _deliver, _refusal, _session_id
        from tools.controller.owner_ask_action import _user
        user_id = _user(controller, execution_context)
        why = _refusal(controller, execution_context, user_id)
        if why:
            return ActionResult(error=why, include_in_memory=True)
        from core.commerce import store as st
        from core.commerce.offer import OfferError
        op = (params.op or "").strip().lower()
        oid = (params.offer_id or "").strip().lower()
        try:
            if op == "draft":
                o = st.save_draft(user_id, params.manifest or {})
                return _ok(f"Offer {o.id} saved ({o.state}). Card hash "
                           f"{st.card_hash(o)}. Next: write the playbook skill "
                           f"`{o.working().playbook}`, then propose_setup (if it needs "
                           f"contracts or accounts) or record a test and propose_live.")
            if op == "resource":
                r = st.add_resource(oid, user_id, params.kind or "", params.data or {},
                                    chain=params.chain, address=params.address)
                return _ok(f"Resource {r.id} ({r.kind}) recorded on {oid}. It gains authority "
                           f"only when the owner confirms the next live version.")
            if op == "test":
                st.record_test(oid, user_id, params.data or {})
                return _ok(f"Test evidence recorded on {oid}.")
            if op == "pause":
                o = st.set_state(oid, user_id, st.PAUSED, actor="agent")
                return _ok(f"Offer {o.id} is paused; nothing new can be ordered.")
            if op == "status":
                from modules.commerce.owner_verb import offer_reply
                return _ok(await offer_reply(user_id, ["show", oid] if oid else ["list"]))
            if op == "orders":
                from modules.commerce.owner_verb import offer_reply
                return _ok(await offer_reply(user_id, ["orders"] + ([oid] if oid else [])))
            if op in ("propose_setup", "propose_live"):
                o = st.get(oid)
                if o is None or o.user_id != user_id:
                    return ActionResult(error=f"offer_manage: no offer {oid!r}", include_in_memory=True)
                from core.surfaces import cards
                from modules.commerce.owner_verb import live_card_body, setup_card_body
                if op == "propose_setup":
                    if params.budget_usd is None:
                        return ActionResult(error="offer_manage: propose_setup needs budget_usd",
                                            include_in_memory=True)
                    card = cards.job_card(user_id, ["/offer", "setup", o.id, f"{params.budget_usd:g}"],
                                          title=f"Set up offer {o.id}",
                                          body=setup_card_body(o, params.budget_usd),
                                          session_id=_session_id(controller, execution_context) or None)
                else:
                    card = cards.job_card(user_id, ["/offer", "live", o.id, st.card_hash(o)],
                                          title=f"Sell: {o.working().title}", body=live_card_body(o),
                                          session_id=_session_id(controller, execution_context) or None)
                outcome = await _deliver(controller, user_id, card,
                                         _session_id(controller, execution_context))
                return _ok(f"Card {card.card_id} is with the owner ({outcome}). NOTHING changes "
                           f"until the owner taps Confirm; do not say it is live.")
        except (OfferError, st.CommerceError) as exc:
            return ActionResult(error=f"offer_manage: {exc}", include_in_memory=True)
        except st.CommerceUnreadable as exc:
            return ActionResult(error=f"offer_manage: the commerce store could not be read ({exc})",
                                include_in_memory=True)
        return ActionResult(error=f"offer_manage: op must be one of {', '.join(OPS)}",
                            include_in_memory=True)

    class OrderDeliver(BaseModel):
        order_id: str = Field(..., description="The order this run fulfils.")
        proof: Dict[str, Any] = Field(default_factory=dict, description=(
            "tx_receipt: {tx_hash}; file_hash: {sha256}; url: {url}. Optional media_paths: "
            "files sent to the buyer."))
        message: str = Field("", max_length=1500, description="A short note for the buyer.")
        failed: bool = Field(False, description="True when the deliverable could not be made.")
        reason: str = Field("", max_length=500, description="failed=true: why.")

    @controller.registry.action(
        "End an order's fulfillment: record the proof, write the receipt and tell the buyer "
        "(or failed=true with the reason). Only the order's own run or the owner.",
        param_model=OrderDeliver,
    )
    async def order_deliver(params: OrderDeliver, execution_context=None) -> ActionResult:
        from tools.controller.owner_ask_action import _user
        user_id = _user(controller, execution_context)
        why = _deliver_refusal(controller, execution_context, params.order_id)
        if why:
            return ActionResult(error=why, include_in_memory=True)
        from modules.commerce import service
        container = getattr(controller, "container", None)
        try:
            if params.failed:
                o = await service.fail(params.order_id, params.reason or "no reason given",
                                       user_id=user_id, container=container)
                return _ok(f"Order {o.id} is {o.status}. Stop working on it.")
            o = await service.deliver(params.order_id, dict(params.proof or {}),
                                      user_id=user_id, container=container,
                                      message=params.message)
        except service.OrderRefused as exc:
            return ActionResult(error=f"order_deliver: {exc}", include_in_memory=True)
        return _ok(f"Order {o.id} is {o.status}; the receipt is {o.receipt.get('sha256', '')[:16]}…. "
                   f"Your work on this order is done.")


def _deliver_refusal(controller, execution_context, order_id: str) -> Optional[str]:
    from core.surfaces.room_policy import is_public_session
    orch = getattr(controller, "orchestrator", None)
    if is_public_session(orch):
        return "Refused: a room session cannot deliver an order."
    from agents.task.session_class import correspondent_facing
    if correspondent_facing(orch):
        return "Refused: a session holding a correspondent's message cannot deliver an order."
    meta = getattr(execution_context, "metadata", None) or {}
    if str(meta.get("mandate") or "") == str(order_id):
        # One order, ONE run: after a retry the order has a new goal, and the old run
        # may neither deliver nor fail it (the same bar as the money kernel's step 4c).
        from core.autonomy_marker import goal_for_session
        from core.commerce import orders as ords
        o = ords.get(order_id)
        if o is not None and o.goal_id and goal_for_session(
                getattr(execution_context, "session_id", None)) == o.goal_id:
            return None
        return (f"Refused: this run is no longer order {order_id}'s fulfillment (it was "
                f"retried or closed). Stop working on it.")
    from tools.controller.turn_origin import _is_forged_or_autonomous_turn
    try:
        if not _is_forged_or_autonomous_turn(execution_context, None):
            return None  # the owner delivering by hand
    except Exception:
        pass
    return "Refused: only the run fulfilling this order, or the owner, may deliver it."


def _ok(text: str) -> ActionResult:
    return ActionResult(extracted_content=text, include_in_memory=True)


__all__ = ["register_offer_actions"]
