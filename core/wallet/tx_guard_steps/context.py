"""The one context the tx_guard steps share (W6).

The sixteen ``_authorize`` arguments (after their defaults are resolved), then
the values the steps derive, in the order the steps derive them. Mutable on
purpose: a step that falls through writes what a later step reads.
"""
from dataclasses import dataclass
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
    #: step 5: the simulation's measurement.
    deltas: Optional[object] = None
    #: step 6: what the simulation proved about a CREATE.
    deploy_facts: Optional[object] = None
    #: steps 6/7: a paired-leg implied LP valuation.
    lp_implied: bool = False
    #: step 6: the measured outflow.
    outflow_raw: int = 0
    #: step 6: the LP position the simulation minted.
    position_token_id: Optional[int] = None
    #: step 6f: the agent token a registration minted.
    registered_agent_id: Optional[int] = None
    #: step 7: the value the caps are charged.
    amount_usd: Optional[float] = None
    #: step 7: which side supplied the USD estimate.
    valuation_basis: str = "outflow"
