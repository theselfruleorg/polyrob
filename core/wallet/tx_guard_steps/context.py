"""The one context the tx_guard steps share (W6).

The sixteen ``_authorize`` arguments (after their defaults are resolved), then
the values the steps derive, in the order the steps derive them. Mutable on
purpose: a step that falls through writes what a later step reads.
"""
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class GuardContext:
    # -- the arguments ----------------------------------------------------
    intent: object
    tx: dict
    holder: str
    gate: object
    execution_context: object
    tool_self: object
    simulate_fn: Callable
    price_fn: Optional[Callable]
    fallback_price_fn: Optional[Callable]
    rpc_is_pinned_fn: Callable
    halted_fn: Callable
    entry_paused_fn: Callable
    forged_fn: Optional[Callable]
    autonomous_ok_fn: Optional[Callable]
    liquidity_rpc: Optional[Callable]
    account_rpc: Optional[Callable]
    #: step 9: the caller's own ask-above value (the signer); None = the owner's dial.
    approve_above_usd: Optional[float] = None
    #: step 9: a send valued at the weaker price passes inside the caps (the signer).
    unverified_within_caps: bool = False
    # -- derived, in step order ------------------------------------------
    #: step 0: THE OWNER is asking in this turn.
    owner_direct: bool = False
    #: step 0: the owner approved THIS call within its bound.
    owner_granted: bool = False
    #: step 0: the pause probe steps 1 and 8 consult.
    pause_fn: Optional[Callable] = None
    #: step 2: a forged-but-allowed (goal-dispatched) turn.
    autonomous_origin: bool = False
    #: step 2: the DEFI_MONITOR_EXITS lane.
    monitor_exit: bool = False
    #: step 2b: the JournalLog a journaled account call writes.
    journal_to: Optional[str] = None
    #: step 2b: what the treasury key signs (``tx`` may become the inner call).
    outer_tx: Optional[dict] = None
    #: step 2b: the signer under ``via_account`` (``holder`` is then the account).
    sender: Optional[str] = None
    #: step 3: a claim from a pinned sale — ``"claim"`` / ``"settle"`` (``core.wallet.collection_sale``).
    sale_call: Optional[str] = None
    #: step 3: a seller's sale act — ``(kind, arg, target)`` (``core.wallet.tx_guard_account``).
    sale_act: Optional[tuple] = None
    #: step 3: a buyer's call on a pinned market (``core.wallet.tx_guard_sale.BuyerAct``).
    sale_buyer: Optional[object] = None
    #: step 4: sentences the final allow appends to its reason (the sale hold's note).
    notes: list = field(default_factory=list)
    #: step 5: the simulation's measurement.
    deltas: Optional[object] = None
    #: step 6: what the simulation proved about a CREATE.
    deploy_facts: Optional[object] = None
    #: steps 6/7: a paired-leg implied LP valuation.
    lp_implied: bool = False
    #: step 2: the order's recipient when the run fulfils an order (099), else None.
    offer_recipient: Optional[str] = None
    #: step 2: what an order run's transaction does (the kernel's offer step), else None.
    #: Step 2 only CHECKS the order's budget; :func:`turn.reserve_offer_budget` books it
    #: after the final allow, so a transaction a later step refuses books nothing.
    offer_scope: Optional[dict] = None
    #: step 6: the measured outflow.
    outflow_raw: int = 0
    #: step 6: the LP position the simulation minted.
    position_token_id: Optional[int] = None
    #: step 6f: the agent token a registration minted.
    registered_agent_id: Optional[int] = None
    #: step 7: the value the caps are charged.
    amount_usd: Optional[float] = None
    #: step 7: the value the transaction MOVES (no network fee) — what the
    #: declared max_spend_usd and a seat's `$` bound are held to.
    value_usd: Optional[float] = None
    #: step 7: which side supplied the USD estimate.
    valuation_basis: str = "outflow"
    #: step 7: a send valued below spend grade — step 9 lets only the owner run it.
    owner_only: bool = False
