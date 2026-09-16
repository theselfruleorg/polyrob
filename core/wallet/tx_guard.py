"""THE choke point. No value-moving transaction may be broadcast without a
``Decision(allowed=True)`` from this module.

The bound is NOT "we only wrote safe verbs" — that stops being a security
property the moment the agent can supply its own calldata. The bound is: every
state-changing transaction is simulated, its asset and allowance deltas are
asserted against a DECLARED intent, and it is refused if the simulation
disagrees with the declaration. Screening, denylists, caps and approval lanes
are defence in depth layered on that.

What this mechanism genuinely cannot see, recorded so nobody mistakes the bound
for a guarantee:

* **The declaration and the calldata can share an author.** Delta assertion
  proves the transaction does what the intent SAYS; it cannot prove the intent
  is legitimate. If a prompt injection authors both, they will agree. The
  defences against that are turn-origin refusal and the caps, not this.
* **A signature is not a transaction.** An EIP-2612/Permit2 payload is signed
  and submitted by someone else later, so it never reaches this function. That
  is why ``Signer.sign_typed_data`` is kept off the money path.
* **The RPC is the oracle.** Simulation, deltas and caps all read from one
  endpoint. Hence the refusal to arm on the shared public endpoint.

Fail-closed is not configurable here: any probe failure refuses.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence, Tuple

from core.env import bool_env, float_env
from core.wallet import simulation

logger = logging.getLogger(__name__)

_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
_DEAD_ADDRESS = "0x000000000000000000000000000000000000dEaD"

#: Native balance is not expected to move on an ERC-20 transfer (eth_call does
#: not charge gas), so any material native delta means the transaction does
#: something the caller did not declare.
_NATIVE_DUST_WEI = 10 ** 12

#: Added to the autonomous ceiling when an allowance grant cannot be valued, so
#: the charge is STRICTLY above the ceiling and the owner queue is the only lane
#: left. One cent, because the caps are compared in cents.
_UNVALUED_GRANT_EPSILON_USD = 0.01


@dataclass(frozen=True)
class TxIntent:
    """What the caller SAYS the transaction will do. Asserted, never trusted."""
    chain: str
    token: Optional[str]          # None = native transfer
    #: The destination. ``None``/`""` ONLY with ``is_deploy=True`` — a CREATE is
    #: the one transaction shape that has no destination (042).
    to: Optional[str]
    amount_raw: int
    max_spend_usd: float
    expected_allowance_grants: Sequence[Tuple[str, str, int]] = ()
    #: Spenders to MEASURE without declaring a grant (e.g. the router a swap
    #: pulls through). A measured pair is judged by its read delta — increases
    #: refuse unless declared, decreases are fine — while an Approval EVENT on
    #: an unmeasured pair refuses outright. Without this, an OZ-4.x-style
    #: token that re-emits Approval(remaining) on transferFrom would read as a
    #: hidden grant on every legitimate swap.
    watch_spenders: Sequence[str] = ()
    idempotency_key: Optional[str] = None
    #: True for an approve/revoke (023 T4). Such a call transfers NOTHING — its
    #: risk is the allowance, declared above and verified against the simulated
    #: delta. Without this the structural "amount must be > 0" TRANSFER rule
    #: refused every approval outright (found on prod 2026-08-14; the unit tests
    #: stub the guard, so they could not see it).
    is_allowance_op: bool = False
    #: The wallet's OWN on-chain balance of `token` at call time (028, 2026-08-22).
    #: An allowance grant on `token` that does not exceed this is EXIT-bounded —
    #: it cannot put more at risk than the wallet already holds, because the
    #: spender can never move more than the approved amount and the approved
    #: amount is capped here at what is already owned. This lets a priced-at-
    #: entry position that later loses DexScreener "high" confidence still be
    #: approved for exit (fallback_price_fn values it for the cap checks below;
    #: the exemption is what removes the *requirement* of a high-confidence
    #: price, not the requirement of SOME price). None (default) = no exemption,
    #: byte-identical to pre-028 behaviour — the caller must opt in.
    held_balance_raw: Optional[int] = None
    #: The token the transaction is expected to RECEIVE (a swap's token_out),
    #: declared so the guard can MEASURE it (2026-08-26 exit untying). Two jobs:
    #: (a) when the outflow token has no price by ANY source, an exit within
    #: held balance is valued at the measured inflow of the chain's quote asset
    #: — the treasury's receipt is exact and unfalsifiable, so the caps run
    #: against it instead of dead-ending on "unpriceable"; (b) it is what makes
    #: an intent recognizably EXIT-shaped for the DEFI_MONITOR_EXITS lane.
    #: None (default) = byte-identical legacy behaviour.
    inflow_token: Optional[str] = None
    #: The MINIMUM raw amount of `inflow_token` the transaction must be measured
    #: to return (042, the assertion proposal 038 §3.3 named as the one thing
    #: missing before caller-supplied calldata could be assertable).
    #:
    #: Until this existed, `inflow_token` only fed VALUATION: a call that took
    #: USDC and handed back nothing cleared every check — the outflow was within
    #: the declared amount, no undeclared transfer, no undeclared approval. For a
    #: curated swap the route provider's own min-out covers that gap; for an
    #: arbitrary contract call NOTHING did.
    #:
    #: Declaring it makes the SIMULATION, not the caller, decide whether the
    #: receipt arrived. Requires `inflow_token` — a minimum with no token to
    #: measure is a caller bug and refuses rather than passing vacuously.
    #: None (default) = no requirement, byte-identical to pre-042.
    min_inflow_raw: Optional[int] = None
    #: The MINIMUM native (wei) the transaction must be measured to RETURN, for
    #: a trade that sends a TOKEN and receives the chain's native asset (042).
    #:
    #: Without it such a trade is unrepresentable: rule 6b refuses any material
    #: native movement alongside a declared token outflow ("the transaction does
    #: something that was not declared"), which is right for a transfer and
    #: wrong for a sell into a native-quoted venue — the same blind spot that
    #: made `routes/lifi.py` refuse every native-value quote. Declaring it
    #: REPLACES that blanket refusal with an assertion: the native delta must be
    #: positive and at least this large.
    #: None (default) = the pre-042 refusal, unchanged.
    min_native_inflow_wei: Optional[int] = None
    #: This transaction CLAIMS value already owed to the wallet — a creator-fee
    #: escrow, a vesting release, any credit a protocol is holding for us.
    #:
    #: A third intent shape, because every existing branch refused it for a
    #: different reason: step 3 refuses ``amount_raw == 0``; the native branch
    #: refuses a positive delta outright ("simulation shows a native INFLOW for
    #: a send"); and the 042 native-inflow assertion is keyed on
    #: ``intent.token``, so on a ``token=None`` intent a declared minimum would
    #: have been silently SKIPPED — a declaration that is not enforced reads
    #: exactly like one that is, which is the worse failure.
    #:
    #: Found live 2026-09-14: 13.613 ETH of creator tax credited to the agent's
    #: own wallet in a launchpad fee escrow, with no verb able to reach it.
    #:
    #: A claim MUST declare what it expects to receive (``min_native_inflow_wei``
    #: or ``inflow_token`` + ``min_inflow_raw``) — a claim that asserts nothing
    #: is not a claim — and NOTHING may leave. Its cost is the FEE, never $0.00.
    is_claim: bool = False
    #: This transaction CREATES a contract (042). It has no destination, its
    #: ``amount_raw`` is the native value endowed to the constructor (commonly
    #: zero), and its proof is the runtime bytecode the simulation returns —
    #: measured by :mod:`core.wallet.deploy_guard`, adjudicated here.
    is_deploy: bool = False
    #: The init code, declared so the guard can bound it (EIP-3860) rather than
    #: reading it back out of the transaction it is meant to be checking.
    init_code: Optional[str] = None
    #: The runtime bytecode this deployment MUST produce, when deploying a pinned
    #: template. None = caller-supplied bytecode: still bounded and still
    #: required to produce code, but nothing to compare against.
    expected_runtime: Optional[str] = None
    #: ``(offset, length)`` byte ranges of ``expected_runtime`` the compiler
    #: fills at construction time (solc ``immutableReferences``) — the only bytes
    #: allowed to differ from the template.
    immutable_slots: Sequence[Tuple[int, int]] = ()
    #: A 32-byte CREATE2 salt. When set, the deployment goes through the pinned
    #: deterministic factory (``deploy_guard.CREATE2_FACTORY``) instead of a bare
    #: CREATE, so ``to`` IS that factory and the address is the same on every
    #: chain for the same bytes.
    #:
    #: The proof changes shape and gets STRONGER: the address is
    #: ``keccak(0xff ++ factory ++ salt ++ keccak(init_code))``, a cryptographic
    #: commitment to the init code, so the factory returning the address we
    #: computed from OUR bytes proves what was deployed without reading a single
    #: byte of runtime back.
    create2_salt: Optional[str] = None
    #: This transaction moves a NON-FUNGIBLE (2026-09-15). The FOURTH shape,
    #: after a spend, a deploy and a claim — and like each of those it exists
    #: because every other branch refused it for a different reason: step 3
    #: refuses ``amount_raw == 0``, ``intent.token`` is None so no fungible
    #: branch runs, and what moves is an IDENTIFIER, which no amount rule can
    #: describe. Exactly the ``is_allowance_op`` situation of 2026-08-14.
    #:
    #: ⚠️ An NFT is UNPRICEABLE. Its cost is the worst-case FEE, never $0.00 —
    #: and because the caps cannot bound it, the OWNER does: the verbs sit in
    #: ``spend_lane.ALWAYS_OWNER_APPROVED_VERBS``, so no flag can exempt them.
    is_nft_op: bool = False
    #: What the caller SAYS will LEAVE: ``(contract, standard, token_id, amount)``
    #: — ``standard`` is ``"erc721"``/``"erc1155"``; ``amount`` is 1 for 721.
    #: Held to the simulation in both directions: an undeclared move refuses,
    #: and a declared move the simulation does not emit refuses too.
    nft_out: Sequence[Tuple[str, str, int, int]] = ()
    #: What the caller SAYS must ARRIVE, same shape. The 042 ``min_inflow_raw``
    #: assertion for the shape that carries no amount.
    expected_nft_in: Sequence[Tuple[str, str, int, int]] = ()
    #: ``ApprovalForAll`` states this call deliberately changes:
    #: ``(contract, operator, approved)``.
    #:
    #: ⚠️ ONLY ``approved=False`` (a REVOKE) may be declared. A blanket grant is
    #: a standing claim on every token of a collection, present and future, and
    #: no simulation can bound what it later enables — it is the single most
    #: common way a self-custodial wallet is drained. No verb in this tree may
    #: author one, so declaring ``True`` does not buy it through; it refuses.
    nft_operator_ops: Sequence[Tuple[str, str, bool]] = ()
    #: This transaction registers the agent's identity on an ERC-8004 Identity
    #: Registry (046). The SIXTH shape, and it exists for the same reason each
    #: of the five before it did: `amount_raw == 0`, `token=None`, no
    #: counterparty to assert against, and -- the one that matters -- nothing
    #: checking that the registry MINTED us anything. A registration sends no
    #: value, so without the receipt assertion below the guard cannot tell "I
    #: registered" from "I called a contract that took my gas".
    is_registration: bool = False
    #: The registry that MUST mint the agent token to us.
    #:
    #: ⚠️ This comes from `core.wallet.erc8004`'s PINNED table, never from the
    #: caller or an env var. It is both the destination check and the receipt
    #: check, which together are what stop this verb from being a generic
    #: "call an arbitrary contract from the treasury wallet".
    expected_registry: Optional[str] = None
    #: Whether this registration transaction is expected to MINT.
    #:
    #: ``True`` (register) -- rule 6f requires Transfer(0x0 -> us) from the
    #: registry. ``False`` (setAgentURI, an update) -- it asserts the exact
    #: INVERSE: nothing may be minted, because an "update" that mints leaves a
    #: SECOND agentId and no way to say which identity is authoritative. Both
    #: directions are checked; neither passes vacuously.
    expects_mint: bool = True
    is_liquidity_op: bool = False
    lp_outflows: Sequence[Tuple[Optional[str], int]] = ()
    lp_inflows: Sequence[Tuple[Optional[str], int]] = ()
    lp_held_balances: Sequence[Tuple[Optional[str], int]] = ()
    lp_position: Optional[Tuple[str, Optional[int]]] = None
    lp_position_effect: str = "hold"
    expected_events: Sequence[Tuple[str, str]] = ()


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    lane: str = "refuse"          # "autonomous" | "owner_queue" | "refuse"
    amount_usd: Optional[float] = None
    #: gasUsed measured by the simulation, carried out so the rail can size
    #: the broadcast gas limit from it (EvmRail.size_gas). None = unmeasured.
    sim_gas_used: Optional[int] = None
    #: What the simulation proved about a CREATE (042): the predicted address,
    #: the runtime size and hash, and whether it matched a pinned template.
    #: ``core.wallet.deploy_guard.DeployFacts``; None for every other shape.
    deploy: Optional[object] = None
    #: The agent token id the simulation proved was minted to us (046). The
    #: ERC-8004 agentId IS the ERC-721 tokenId, and reading it off the
    #: simulation is how the verb learns what it got without a second lookup
    #: that could answer about a different transaction. None for every other
    #: shape.
    agent_id: Optional[int] = None
    position_token_id: Optional[int] = None


def ceiling_scope(execution_context=None):
    """``(user_id, home_dir)`` for an owner-ceiling pref lookup, fail-open.

    Returns ``(None, None)`` when the tenant or the pref home cannot be
    resolved, which makes :func:`autonomous_max_usd` fall back to the env
    value — never to a wider one.
    """
    try:
        user_id = getattr(execution_context, "user_id", None)
        if not user_id:
            # The ONE owner-tenant resolver: the console writes the ceiling pref
            # under the tenant it reads, so a ceiling must not stop applying (the
            # env value that then applies can be WIDER).
            from core.instance import resolve_owner_user_id
            user_id = resolve_owner_user_id()
        if not user_id:
            return None, None
        from core.paths import polyrob_home
        return user_id, polyrob_home()
    except Exception:
        logger.debug("ceiling scope unresolved; env value applies", exc_info=True)
        return None, None


def autonomous_max_usd(user_id=None, home_dir=None) -> float:
    """Spend that may execute without asking the owner.

    Distinct from the catastrophic per-transaction ceiling, which is a
    loss limit and is min-merged so a pref can only LOWER it. This one is an
    interruption dial: raising it cannot widen maximum loss, because the
    backstop still binds, so the owner may raise it from any seat
    (``budget.defi_autonomous_usd``). It is CLAMPED to the backstop here —
    an autonomous ceiling above the per-tx ceiling would be a number that can
    never be reached, and reporting it would mislead.

    Fail-open to the env value: an unreadable pref store must never widen
    spend authority, and it must not break the guard either.
    """
    env_value = float_env("DEFI_AUTONOMOUS_MAX_USD", 25.0)
    value = env_value
    if user_id and home_dir is not None:
        try:
            from core import prefs
            resolved = prefs.resolve("budget.defi_autonomous_usd", user_id,
                                     home_dir, env_value=env_value,
                                     default=env_value)
            if resolved is not None:
                value = float(resolved)
        except Exception:
            logger.debug("autonomous-ceiling pref unreadable; using env",
                         exc_info=True)
            value = env_value
    try:
        from core.wallet.config import effective_max_per_tx_usd
        backstop = effective_max_per_tx_usd(user_id, home_dir)
    except Exception:
        backstop = float_env("AGENT_WALLET_MAX_PER_TX_USD", 250.0)
    return min(value, backstop)


def rpc_is_pinned(chain: str) -> bool:
    """True when the operator has pinned a real endpoint for *chain*.

    Money paths refuse to arm on the shared public default: it is
    unauthenticated and rate-limited, and it is simultaneously the source of the
    simulation, the deltas and the balances the caps are computed from.
    """
    return bool(os.getenv(f"DEFI_EVM_RPC_{chain.upper()}", "").strip())


def _halted() -> bool:
    from core.config_policy import AutonomyConfig
    return AutonomyConfig.autonomy_halted()


def _entry_paused() -> bool:
    from core.config_policy import AutonomyConfig
    return AutonomyConfig.entry_paused()


# NOTE: turn-origin detection lives in the TOOLS tier
# (`tools/controller/action_registration.py::_is_forged_or_autonomous_turn`), and
# core (tier 0) may not import tools (tier 3) — the layering ratchet's upward
# allowlist may only shrink. So the caller INJECTS `forged_fn`. When an
# execution_context is present and no detector was supplied we cannot prove the
# turn is genuine, so we refuse: fail-closed by construction rather than by
# remembering to pass an argument.


def autonomous_turn_trading_enabled() -> bool:
    """Whether a GOAL/CRON-dispatched run may move funds (default OFF).

    ``forged_fn`` answers one question — "is this turn anything other than a
    genuine owner turn?" — and step 2 refuses on it. That is right for a leaf
    sub-agent, a self-wake and a delegation-result re-entry, but it also covers
    a goal-dispatched run, which is what an unattended treasury loop IS. With
    this OFF (the default) the agent can only trade when the owner is driving.

    ⚠️ Turning it ON is a real widening of the money surface, not a formality.
    An autonomous run reads untrusted material (web pages, social posts, market
    APIs) in the SAME session that holds the money verb, so an indirect prompt
    injection reaches a signing path it otherwise never could. What bounds the
    damage is not this gate but the caps underneath it — the per-transaction
    ceiling, the rolling daily cap, and the simulation that holds the caller to
    its declared intent. Enable it only on a treasury you can afford to lose
    entirely in one day, and size ``WALLET_DAILY_CAP_USD`` as exactly that
    number.
    """
    return bool_env("DEFI_AUTONOMOUS_TURN_TRADING", False)


def monitor_exits_enabled() -> bool:
    """Whether a forged MAIN-agent turn (self-wake / delegation-result — the
    monitor loop) may execute EXIT-shaped operations (default OFF).

    A week of prod logs (2026-08-25/26) showed stop rules firing on monitor
    ticks and the turn-origin bar refusing the very exit the rule demanded —
    the position then rode further down until the next goal-dispatched run.
    The worst an adversary steering a forged turn can do through this lane is
    close a position EARLY into the treasury's own quote-asset balance: a
    market cost, not a theft. Entries, transfers, and over-held grants stay
    refused; the caps, the simulation and the delta assertion all still apply,
    and the lane rides the autonomous origin so it also demands a daily cap.
    """
    return bool_env("DEFI_MONITOR_EXITS", False)


def _chain_quote_asset(chain: str) -> Optional[str]:
    """The chain's pinned quote asset: USDC, else the wrapped native (a chain
    like robinhood quotes in WETH and pins no stablecoin)."""
    try:
        from core.wallet import chains
        row = chains.get(chain)
    except Exception:
        return None
    if row is None:
        return None
    return row.usdc or row.wrapped_native


def pinned_route_spenders(chain: str) -> frozenset:
    """The spenders a legitimate swap leg may be approved to on *chain*.

    The chain registry already pins both — ``univ3_router`` (a locally built
    route IS the registry's own router) and ``aggregator_spender`` (a third
    party names its own, and ``providers/routes`` refuses any quote whose
    spender is not this exact address). So this adds no new trust: it reads the
    same two pins the route layer already holds every swap to.

    Empty means the chain pins neither, which is a refusal to treat ANY spender
    as a route — never a fallback to "then everybody qualifies".
    """
    try:
        from core.wallet import chains
        row = chains.get(chain)
    except Exception:
        return frozenset()
    if row is None:
        return frozenset()
    from core.wallet.dex_registry import row_for
    dex = row_for(chain, "v3")
    return frozenset(a.lower() for a in (
        row.univ3_router, row.aggregator_spender,
        dex.position_manager if dex else None) if a)


def _grant_is_exit_bounded(intent: TxIntent, g_token: str, g_spender: str,
                           g_amount: int) -> bool:
    """Is this DECLARED grant the approve leg of an exit from a held position?

    Three conditions, all necessary (S3, 2026-09-14 — the first was the whole
    test until then):

    * it is on the token the intent declares, and cannot exceed what the wallet
      already holds of it, so it puts no NEW value at risk (028);
    * its spender IS the destination the intent declared — a grant to somebody
      else is not the transaction the caller described;
    * that spender is a route spender the chain pins, because the exit this
      exemption exists for is the sell leg and the sell leg approves the router.

    Without the last two, "approve the wallet's entire holding of a just-
    launched coin to an arbitrary address" satisfied the exemption, was valued
    at $0.00, cleared every cap and ran on the autonomous lane.
    """
    if intent.held_balance_raw is None:
        return False
    if g_token.lower() != (intent.token or "").lower():
        return False
    if g_amount > intent.held_balance_raw:
        return False
    spender = (g_spender or "").lower()
    declared = (intent.to or "").lower()
    if not spender or spender != declared:
        return False
    pinned = pinned_route_spenders(intent.chain)
    return spender in pinned


def _intent_is_exit_shaped(intent: TxIntent) -> bool:
    """True when the DECLARED intent can only CLOSE a position, never open one.

    - a revoke (allowance op, no grants) is always an exit;
    - an approve whose every grant is on the held token and within the held
      balance is exit-bounded;
    - a swap that sends the held token (within held balance) and declares the
      chain's QUOTE ASSET as its inflow is a sell-to-quote.
    The declaration is not trusted on its own: the simulation still asserts the
    deltas, and the sell case additionally requires a measured quote inflow.
    """
    if intent.is_liquidity_op:
        return (not intent.lp_outflows and bool(intent.lp_inflows)
                and any(n > 0 for _, n in intent.lp_inflows))
    if intent.is_allowance_op:
        if not intent.expected_allowance_grants:
            return True  # revoke
        if intent.held_balance_raw is None:
            return False
        return all(
            g_token.lower() == (intent.token or "").lower()
            and g_amount <= intent.held_balance_raw
            for (g_token, _s, g_amount) in intent.expected_allowance_grants)
    quote = _chain_quote_asset(intent.chain)
    return bool(
        quote and intent.inflow_token
        and intent.inflow_token.lower() == quote.lower()
        and intent.token
        and intent.token.lower() != quote.lower()
        and intent.held_balance_raw is not None
        and intent.amount_raw <= intent.held_balance_raw)


def _autonomous_turn_allowed(execution_context, tool_self, autonomous_ok_fn) -> bool:
    """True only for a goal/cron-dispatched MAIN-agent turn, with the flag on.

    Fails CLOSED on every uncertainty: flag off, no detector supplied, detector
    says no, or detector raises. The detector is injected for the same layering
    reason ``forged_fn`` is, and it must be STRICTER than ``forged_fn`` — it
    answers "is this specifically an autonomous goal run?", never merely "is
    this not a genuine owner turn?".
    """
    if not autonomous_turn_trading_enabled():
        return False
    if autonomous_ok_fn is None:
        return False
    try:
        return bool(autonomous_ok_fn(execution_context, tool_self))
    except Exception:
        return False


def _record_money_refusal(reason: str, tool_self, execution_context,
                          *, detail: str = "") -> None:
    """Typed lane-2 emit for a money-verb refusal. Fail-open by construction —
    ``record_refusal`` swallows, and this wrapper never lets an attribute lookup
    on a caller-supplied object change a guard OUTCOME."""
    try:
        from core.security.refusals import record_refusal
        record_refusal(
            reason,
            tool=getattr(tool_self, "name", None) or "",
            user_id=getattr(execution_context, "user_id", None) or "",
            session_id=getattr(execution_context, "session_id", None) or "",
            detail=detail)
    except Exception:
        logger.debug("tx_guard: refusal record skipped", exc_info=True)


def authorize(intent: TxIntent, tx: dict, *, holder: str, gate,
              execution_context=None, tool_self=None,
              simulate_fn: Optional[Callable] = None,
              price_fn: Optional[Callable] = None,
              fallback_price_fn: Optional[Callable] = None,
              rpc_is_pinned_fn: Optional[Callable] = None,
              halted_fn: Optional[Callable] = None,
              entry_paused_fn: Optional[Callable] = None,
              forged_fn: Optional[Callable] = None,
              autonomous_ok_fn: Optional[Callable] = None,
              liquidity_rpc: Optional[Callable] = None) -> Decision:
    """Authorize *tx* against *intent*. Returns a Decision; never broadcasts.

    The caller must hold ``gate.reserve()`` across authorize → broadcast →
    record so a concurrent transaction cannot both clear a nearly-exhausted cap.
    """
    simulate_fn = simulate_fn or (lambda **kw: simulation.simulate(**kw))
    rpc_is_pinned_fn = rpc_is_pinned_fn or rpc_is_pinned
    halted_fn = halted_fn or _halted
    entry_paused_fn = entry_paused_fn or _entry_paused


    # -- 1. Owner pause (fail closed) --------------------------------------
    # ⚠️ NOT a kill-switch. `halted_fn` is `AutonomyConfig.autonomy_halted()`, which
    # is literally `not allows("dispatch").allowed` — a FACET of the 031 pause
    # record. The legacy text here said "autonomy is HALTED (owner kill-switch)",
    # which named a lever that does not exist and offered no remedy at all.
    # `125f83dd` fixed that wording for the bridge only; every OTHER money verb
    # (swap, solana_swap, transfer, approve_token, revoke_approval) still routed
    # through this branch and still sent the owner hunting for a phantom switch
    # (census, 2026-09-12). One shared renderer now serves both.
    try:
        if halted_fn():
            from core.autonomy_control import pause_refusal_text
            # 045: a money verb refused by the owner's own pause is exactly the
            # kind of unauthorized-action the lane-2 ledger exists to count, and
            # `pause` is already a declared REFUSAL_REASONS slug. It was silent.
            # ⚠️ getattr-with-default, never `intent.chain`: this call sits
            # INSIDE the pause try/except, so an attribute error here would be
            # swallowed as "pause probe failed" — the recorder would have
            # changed the guard's own refusal text. A ledger write must never
            # be able to alter an OUTCOME.
            _record_money_refusal("pause", tool_self, execution_context,
                                  detail=f"chain={getattr(intent, 'chain', '?')}")
            # force=True: `halted_fn` may be the legacy env/touch-file facet or an
            # injected probe, so it can read STOPPED while the record reads allowed.
            # The refusal must carry the same honesty either way.
            return Decision(False, pause_refusal_text(
                "dispatch", what="this transaction", force=True))
    except Exception as exc:
        return Decision(False, f"refused: pause probe failed ({exc}); failing closed")

    # -- 1b. Owner entry-pause (fail closed; exits always pass) ------------
    # 2026-08-28: give the entry-pause a structural home instead of leaving
    # it to each goal's own prose (one stream-manifest goal traded through it
    # unnoticed). Exit-shaped intents (sells to quote, revokes) are never
    # blocked — the pause is about not adding NEW risk.
    try:
        if entry_paused_fn() and not _intent_is_exit_shaped(intent):
            _record_money_refusal(
                "pause", tool_self, execution_context,
                detail=f"entry_pause chain={getattr(intent, 'chain', '?')}")
            return Decision(False, (
                "refused: new treasury entries are PAUSED (owner entry-pause) "
                "— exits still run; clear with `polyrob owner resume-entries`"))
    except Exception as exc:
        return Decision(False, f"refused: entry-pause probe failed ({exc}); failing closed")

    # -- 2. Turn origin ----------------------------------------------------
    autonomous_origin = False
    monitor_exit = False
    if execution_context is not None:
        if forged_fn is None:
            return Decision(False, (
                "refused: an agent turn was supplied but no turn-origin detector — "
                "cannot prove the turn is genuine, so failing closed"))
        try:
            if forged_fn(execution_context, tool_self):
                if not _autonomous_turn_allowed(execution_context, tool_self,
                                                autonomous_ok_fn):
                    # 2026-08-26 exit untying: a forged MAIN-agent turn — the
                    # self-wake monitor loop — may still CLOSE a position when
                    # the operator armed DEFI_MONITOR_EXITS. Stop rules fire on
                    # monitor ticks; refusing the exit here left fired stops
                    # unexecutable until the next goal run. Exit-shaped only,
                    # main agent only; a sell additionally has its measured
                    # quote inflow asserted after simulation.
                    if (monitor_exits_enabled()
                            and not getattr(execution_context, "is_sub_agent", False)
                            and getattr(execution_context, "role", "leaf") == "orchestrator"
                            and _intent_is_exit_shaped(intent)):
                        monitor_exit = True
                        logger.info(
                            "tx_guard.monitor_exit chain=%s token=%s — forged turn "
                            "allowed for an EXIT-shaped operation (DEFI_MONITOR_EXITS)",
                            intent.chain, intent.token)
                    else:
                        from core.security.refusals import record_refusal
                        record_refusal(
                            "forged_turn",
                            tool=getattr(tool_self, "name", None) or "",
                            user_id=getattr(execution_context, "user_id", None) or "")
                        return Decision(False, (
                            "refused: a forged/autonomous turn (self-wake, delegation-result, "
                            "leaf, or autonomous run) cannot move funds"))
                # A genuine owner turn is not forged; only the unattended
                # goal-dispatched lane (or the monitor-exit lane above) reaches
                # here forged-but-allowed. Remember it so step 9 can demand an
                # aggregate damage bound (a daily cap) that an owner-driven
                # trade doesn't need.
                autonomous_origin = True
        except Exception as exc:
            return Decision(False, f"refused: could not prove the turn is genuine ({exc})")

    from core.wallet.authority import turn_refusal
    principal_error = turn_refusal(execution_context)
    if principal_error:
        return Decision(False, f"refused: {principal_error}")

    # -- 3. Structural -----------------------------------------------------
    if intent.amount_raw < 0:
        return Decision(False, "refused: amount cannot be negative")
    if intent.amount_raw == 0 and not (intent.is_allowance_op or intent.is_deploy
                                       or intent.is_claim or intent.is_nft_op
                                       or intent.is_registration or intent.is_liquidity_op):
        # A zero-value transfer is meaningless. An allowance op legitimately
        # moves nothing and must say so explicitly — it is never inferred. A
        # deployment commonly endows the constructor with nothing at all, and
        # its risk is the fee and the code, not a transferred amount. A CLAIM
        # sends nothing by definition; its whole shape is the receipt. An NFT
        # op moves an IDENTIFIER, which no amount can describe.
        return Decision(False, "refused: amount must be greater than zero")

    if intent.is_liquidity_op:
        if execution_context is not None and (getattr(execution_context, "is_sub_agent", False)
                or getattr(execution_context, "role", None) == "leaf"):
            return Decision(False, "refused: a delegated leaf cannot change liquidity")
        from core.wallet import liquidity_guard
        try:
            liquidity_guard.structural(intent, tx)
        except (ValueError, TypeError, AttributeError) as exc:
            return Decision(False, f"refused: {exc}")

    if intent.is_registration:
        # Declared and PINNED, never inferred, and checked HERE so a malformed
        # one cannot reach a measurement branch written for another shape.
        if intent.is_deploy or intent.is_claim or intent.is_nft_op:
            return Decision(False, (
                "refused: a transaction cannot be both an agent REGISTRATION "
                "and another shape -- they assert different things about what "
                "moves and what arrives"))
        if not intent.expected_registry:
            return Decision(False, (
                "refused: a registration must name the registry that will mint "
                "the agent token (`expected_registry`, from the pinned "
                "core.wallet.erc8004 table). One that declares no registry "
                "asserts nothing, and is an arbitrary contract call wearing a "
                "registration's name"))
        # ⚠️ Check the TRANSACTION's destination, not the intent's. The intent
        # is the caller's claim; `tx["to"]` is what actually gets signed, and
        # comparing the claim against itself asserts precisely nothing. (Caught
        # by its own test: the first version of this rule did exactly that and
        # happily authorized a transaction addressed somewhere else entirely.)
        _want_registry = intent.expected_registry.lower()
        _tx_to = str((tx or {}).get("to") or "").lower()
        if intent.to and intent.to.lower() != _want_registry:
            return Decision(False, (
                f"refused: the intent is addressed to {intent.to} but the "
                f"declared ERC-8004 registry is {intent.expected_registry} -- "
                f"a registration may only ever call its pinned registry"))
        if _tx_to != _want_registry:
            return Decision(False, (
                f"refused: the TRANSACTION is addressed to {_tx_to or '(none)'} "
                f"but the declared ERC-8004 registry is "
                f"{intent.expected_registry} -- the signed destination is what "
                f"matters, and it is not the pinned registry"))

    if intent.is_nft_op:
        # Declared, never inferred — and checked HERE so a malformed one cannot
        # reach a measurement branch written for another shape and be waved
        # through by it.
        if intent.is_deploy or intent.is_claim:
            return Decision(False, (
                "refused: a transaction cannot be both an NFT operation and a "
                "DEPLOYMENT or a CLAIM — they assert different things about "
                "what moves"))
        if not (intent.nft_out or intent.expected_nft_in
                or intent.nft_operator_ops):
            return Decision(False, (
                "refused: an NFT operation must declare what leaves "
                "(`nft_out`), what must arrive (`expected_nft_in`), or the "
                "approval it retires — one that declares nothing asserts "
                "nothing, and is an arbitrary call wearing an NFT verb's name"))
        for _c, _op, _approved in intent.nft_operator_ops:
            if _approved:
                return Decision(False, (
                    f"refused: a transaction may never GRANT ApprovalForAll "
                    f"({_op} on {_c}) — a blanket operator approval is a "
                    f"standing claim on every token of the collection, present "
                    f"and future, and no simulation can bound what it later "
                    f"enables. Only a revoke (approved=False) may be declared"))

    if intent.is_claim:
        # A claim is declared, never inferred, and the declaration is checked
        # HERE so a malformed one cannot reach the measurement branches and be
        # waved through by a rule that was written for another shape.
        if intent.is_deploy:
            return Decision(False, (
                "refused: a transaction cannot be both a CLAIM and a DEPLOYMENT "
                "— they assert opposite things about the destination"))
        if intent.is_allowance_op:
            return Decision(False, (
                "refused: a transaction cannot be both a CLAIM and an allowance "
                "operation"))
        if not intent.to:
            return Decision(False, (
                "refused: a claim must name the contract it claims FROM"))
        if str(tx.get("to") or "").lower() != intent.to.lower():
            # The guard authorizes `intent.to`; the rail signs `tx["to"]`. The
            # deploy paths cross-check these and nothing else did. It matters
            # most here because `launchpad_claim` PRINTS a provenance sentence
            # about its destination ("read FROM the curve, not supplied"), and
            # that has to be true of the transaction that gets signed.
            return Decision(False, (
                f"refused: the claim declares {intent.to} but the transaction "
                f"is addressed to {tx.get('to')!r} — the authorized destination "
                f"and the signed one must be the same contract"))
        if intent.token is not None:
            return Decision(False, (
                "refused: a claim declares no outflow, so it carries no outflow "
                "token — name what you EXPECT TO RECEIVE with inflow_token "
                "instead"))
        if intent.amount_raw != 0:
            return Decision(False, (
                f"refused: a claim declared an outflow of {intent.amount_raw} — "
                f"a claim sends nothing, and value that leaves belongs in a "
                f"transfer or a swap where it can be priced"))
        declared_native = (intent.min_native_inflow_wei or 0) > 0
        declared_token = (intent.inflow_token is not None
                          and (intent.min_inflow_raw or 0) > 0)
        if not (declared_native or declared_token):
            # The whole point. A claim that declares no minimum asserts nothing,
            # and an unasserted claim is a call to an arbitrary contract wearing
            # a claim's name.
            return Decision(False, (
                "refused: a claim must declare the MINIMUM it expects to "
                "receive — min_native_inflow_wei, or inflow_token with "
                "min_inflow_raw. A claim that asserts nothing is not a claim"))
    if intent.is_deploy and intent.create2_salt:
        # The DETERMINISTIC path. This one IS a call — to the pinned factory and
        # to nothing else, which is the whole of what makes the address a proof.
        from core.wallet import deploy_guard as _dg
        factory = _dg.CREATE2_FACTORY.lower()
        if (intent.to or "").lower() != factory:
            return Decision(False, (
                f"refused: a CREATE2 deployment must be addressed to the pinned "
                f"factory {_dg.CREATE2_FACTORY}, not {intent.to!r}"))
        if str(tx.get("to") or "").lower() != factory:
            return Decision(False, (
                f"refused: the transaction is addressed to {tx.get('to')!r}, not "
                f"the pinned CREATE2 factory the intent declares"))
        if intent.token is not None:
            return Decision(False, (
                "refused: a deployment endows NATIVE value only — declare "
                "token=None"))
    elif intent.is_deploy:
        if intent.to:
            return Decision(False, (
                f"refused: a deployment has no destination, but this intent "
                f"declares {intent.to!r} — a call wearing a deploy's name is "
                f"not a deploy"))
        if tx.get("to"):
            return Decision(False, (
                "refused: the transaction carries a destination, so it is a "
                "CALL, not the CREATE this intent declares"))
        if intent.token is not None:
            return Decision(False, (
                "refused: a deployment endows NATIVE value only — declare "
                "token=None. A token cannot be sent by a create transaction"))
    elif not intent.to:
        return Decision(False, (
            "refused: no destination declared, and this intent is not a deploy"))
    elif intent.to.lower() in (_ZERO_ADDRESS.lower(), _DEAD_ADDRESS.lower()):
        return Decision(False, "refused: destination is the zero/burn address")

    # -- 4. RPC trust ------------------------------------------------------
    if not rpc_is_pinned_fn(intent.chain):
        return Decision(False, (
            f"refused: no pinned RPC for {intent.chain}. Simulation, deltas and caps "
            f"all read from it, so the shared public endpoint cannot be the trust "
            f"anchor for moving funds — set DEFI_EVM_RPC_{intent.chain.upper()}"))

    if intent.is_liquidity_op:
        from core.wallet import dex_registry, abi
        rpc = liquidity_rpc or simulation._default_rpc_for(intent.chain)
        try:
            dex_registry.verify_pins(rpc, intent.chain, "v3")
            if intent.lp_position_effect != "mint":
                raw = rpc("eth_call", [{"to": intent.to, "data": abi.encode_call(
                    "ownerOf", [{"type": "uint256"}], [intent.lp_position[1]])}, "latest"])
                owner = abi.decode([{"type": "address"}], raw)[0]
                if owner.lower() != holder.lower():
                    raise ValueError("the wallet does not own the liquidity position")
        except Exception as exc:
            return Decision(False, f"refused: liquidity pin/ownership verification failed ({exc})")

    # -- 5. Simulate -------------------------------------------------------
    spenders = sorted({s for (_t, s, _a) in intent.expected_allowance_grants}
                      | set(intent.watch_spenders))
    tokens = [t for t in (intent.token, intent.inflow_token) if t]
    if intent.is_liquidity_op:
        tokens = sorted({t.lower() for t, _ in (*intent.lp_outflows, *intent.lp_inflows) if t})
    try:
        deltas = simulate_fn(tx=tx, holder=holder, chain=intent.chain,
                             tokens=tokens, spenders=spenders)
    except Exception as exc:
        return Decision(False, f"refused: simulation raised ({exc})")
    if not deltas.ok:
        return Decision(False, f"refused: simulation not trustworthy — {deltas.error}")

    # -- 6. Assert the deltas against the declaration ----------------------
    outflow_raw = 0
    deploy_facts = None
    position_token_id = None
    lp_implied = False
    registered_agent_id = None  # set by rule 6f when a registration is proven
    if intent.is_deploy:
        # A CREATE (042). There is no counterparty to assert against, so the
        # assertions are about the CODE it produces and about the constructor
        # moving nothing it did not declare — measured in deploy_guard, decided
        # here. The native branch below cannot serve: it refuses a zero outflow
        # as a measurement failure, and a deployment endowing nothing is the
        # normal case, not a broken measurement.
        from core.wallet import deploy_guard as _deploy_guard

        deploy_facts, why = _deploy_guard.assert_deploy(
            intent, deltas, holder=holder, nonce=tx.get("nonce"))
        if why:
            return Decision(False, why)
        moved = int(deltas.native_delta)
        if moved > 0:
            return Decision(False, (
                f"refused: the constructor returns {moved} wei to the wallet — a "
                f"deployment that pays the deployer is not the transaction that "
                f"was declared"))
        outflow_raw = -moved
        if outflow_raw > intent.amount_raw + _NATIVE_DUST_WEI:
            return Decision(False, (
                f"refused: the deployment moves {outflow_raw} wei but declared "
                f"{intent.amount_raw} — the constructor spends more than it was "
                f"endowed with"))
    elif intent.is_liquidity_op:
        try:
            position_token_id = liquidity_guard.assert_deltas(intent, deltas, _NATIVE_DUST_WEI)
        except (ValueError, TypeError) as exc:
            return Decision(False, f"refused: {exc}")
    elif intent.token:
        moved = deltas.token_deltas.get(intent.token)
        if moved is None:
            return Decision(False, "refused: simulation did not measure the token being sent")
        if intent.is_allowance_op:
            # An approve/revoke changes an allowance and must move NOTHING.
            # (Before this branch the zero delta below read as a measurement
            # failure, so with a pinned RPC every approve/revoke was dead on
            # arrival — the second DOA hole on this path; the first was the
            # structural amount>0 rule, fixed by is_allowance_op itself.)
            if moved != 0:
                return Decision(False, (
                    f"refused: an allowance operation moved {moved} of the token — "
                    f"an approve/revoke must not transfer funds"))
        else:
            if moved > 0:
                return Decision(False, "refused: simulation shows an INFLOW for a send")
            if moved == 0:
                # A send that moves nothing means the MEASUREMENT failed, not that
                # the transfer is free. This exact hole broadcast a live 0.25 USDC
                # transfer on 2026-08-09: eth_call does not persist state, so every
                # delta read back as 0, the outflow priced at $0.00, and it sailed
                # through a cap that should have refused it. Zero is never a cheap
                # transfer — it is an unmeasured one.
                return Decision(False, (
                    "refused: the simulation measured NO outflow for a transfer that "
                    "should move funds — treating that as a measurement failure, not "
                    "as a free transaction"))
            outflow_raw = -moved
            if outflow_raw > intent.amount_raw:
                return Decision(False, (
                    f"refused: simulated outflow {outflow_raw} exceeds the declared "
                    f"amount {intent.amount_raw}"))
    elif intent.is_claim:
        # A CLAIM (2026-09-14). Nothing leaves, so there is no outflow to bound;
        # what must be proven is that the RECEIPT arrived and that nothing else
        # moved. Both are asserted below, against the measurement.
        outflow_raw = 0
        for l_token, l_moved in (deltas.token_deltas or {}).items():
            if l_moved < 0:
                # A contract that pays you and quietly takes a token is not a
                # claim. Receiving more than declared is fine; giving anything
                # up is not.
                return Decision(False, (
                    f"refused: the claim moves {-l_moved} of token {l_token} OUT "
                    f"of the wallet — a claim sends nothing"))
        if intent.min_native_inflow_wei:
            if deltas.native_delta <= 0:
                return Decision(False, (
                    f"refused: the claim declared a native receipt but the "
                    f"simulation measures a native delta of {deltas.native_delta} "
                    f"wei — nothing arrives"))
            if deltas.native_delta < intent.min_native_inflow_wei:
                return Decision(False, (
                    f"refused: the simulation returns {deltas.native_delta} wei, "
                    f"below the declared minimum {intent.min_native_inflow_wei} — "
                    f"the contract does not pay what it says it owes"))
        if intent.inflow_token is not None and intent.min_inflow_raw:
            got = (deltas.token_deltas or {}).get(intent.inflow_token)
            if got is None:
                return Decision(False, (
                    f"refused: the simulation did not measure token "
                    f"{intent.inflow_token}, so the declared receipt is unproven"))
            if got < intent.min_inflow_raw:
                return Decision(False, (
                    f"refused: the claim returns {got} of {intent.inflow_token}, "
                    f"below the declared minimum {intent.min_inflow_raw}"))
    elif intent.is_registration:
        # Registering sends NO value, so the native branch below cannot serve --
        # it refuses a zero outflow as a measurement failure, and moving nothing
        # is this shape's normal case (same reasoning as is_deploy/is_nft_op).
        # What IS asserted is the RECEIPT, in rule 6f. Here: nothing may leave.
        if abs(int(deltas.native_delta)) > _NATIVE_DUST_WEI:
            return Decision(False, (
                f"refused: the registration moved {deltas.native_delta} wei of "
                f"native value -- `register()` is not payable on the reference "
                f"registries, so unexplained native movement is not this "
                f"transaction"))
    elif intent.is_nft_op:
        # A non-fungible move (2026-09-15). Nothing fungible leaves, so the
        # native branch below cannot serve — it refuses a zero outflow as a
        # measurement failure, and moving nothing fungible is this shape's
        # NORMAL case, not a broken measurement (same reasoning as is_deploy).
        #
        # What IS asserted: no material native movement. `safeTransferFrom`
        # is not payable in any standard implementation, so native leaving here
        # means the call did something the intent never described. The token
        # side is asserted by rules 6c/6d below, and rule 6b already refuses
        # any undeclared FUNGIBLE transfer out.
        if abs(int(deltas.native_delta)) > _NATIVE_DUST_WEI:
            return Decision(False, (
                f"refused: an NFT transfer moved {deltas.native_delta} wei of "
                f"native value — the intent declares a collectible move and "
                f"nothing else, so unexplained native movement is not this "
                f"transaction"))
    elif intent.is_allowance_op:
        # An allowance is a claim on a TOKEN. A native asset has no allowance to
        # grant or revoke, so this combination is a caller bug, not a shape to
        # interpret.
        return Decision(False, (
            "refused: an allowance operation needs a token — a native asset has no "
            "allowance to grant or revoke"))
    else:
        # NATIVE send (039 B1). `intent.token is None` is a DECLARATION, not an
        # absence. Before this branch the guard was ERC-20-only by construction:
        # the outflow assertions above are keyed on `intent.token`, so a native
        # send skipped them all and then hit the blanket native-dust refusal
        # below — which is why `bridge_verb.py` carried a bare "an EVM-origin
        # bridge is not wired yet". A Relay EVM deposit IS a native-value call.
        #
        # Fee and principal are the SAME ASSET here, exactly as they are on the
        # Solana leg, so an exact match cannot be demanded. The tolerance is the
        # SAME pinned dust constant the ERC-20 branch uses below — deliberately
        # not a new number, and deliberately symmetric: a short move and a long
        # move are both "not the transaction that was priced".
        moved = int(deltas.native_delta)
        if moved > 0:
            return Decision(False, "refused: simulation shows a native INFLOW for a send")
        if moved == 0:
            # Same lesson as the token branch, restated for native: a live 0.25
            # USDC transfer broadcast on 2026-08-09 because every delta read back
            # 0 and the outflow priced at $0.00. Zero is never a free transaction
            # — it is an unmeasured one.
            return Decision(False, (
                "refused: the simulation measured NO native outflow for a send that "
                "should move funds — treating that as a measurement failure, not as "
                "a free transaction"))
        outflow_raw = -moved
        if outflow_raw < intent.amount_raw - _NATIVE_DUST_WEI:
            return Decision(False, (
                f"refused: the simulation moves only {outflow_raw} wei of the declared "
                f"{intent.amount_raw} — a send that moves materially less than it "
                f"declared is not the transaction that was priced"))
        if outflow_raw > intent.amount_raw + _NATIVE_DUST_WEI:
            return Decision(False, (
                f"refused: simulated native outflow {outflow_raw} exceeds the declared "
                f"amount {intent.amount_raw} by more than fee dust"))

    declared = {(t.lower(), s.lower()): a for (t, s, a) in intent.expected_allowance_grants}
    for (token, spender), change in deltas.allowance_deltas.items():
        if change <= 0:
            continue
        permitted = declared.get((token.lower(), spender.lower()))
        if permitted is None:
            return Decision(False, (
                f"refused: UNDECLARED allowance grant of {change} on {token} to "
                f"{spender} — a hidden approve is not bounded by a USD cap, because "
                f"the drain happens in a later transaction"))
        if change > permitted:
            return Decision(False, (
                f"refused: allowance grant {change} exceeds the declared {permitted}"))

    # -- 6a. Assert the declared MINIMUM INFLOW ---------------------------
    # 042. The other half of the delta contract: everything above bounds what
    # LEAVES, this bounds what must COME BACK. It is what makes caller-supplied
    # calldata assertable at all — a swap/supply/mint call that spends and
    # returns nothing is indistinguishable, to every check above, from one that
    # works.
    #
    # Fail-CLOSED in both directions: a minimum with no token to measure is a
    # caller bug (refuse, never pass vacuously), and an unmeasured inflow is a
    # measurement failure (refuse), exactly as a zero outflow is above.
    if intent.min_inflow_raw is not None:
        if intent.min_inflow_raw <= 0:
            return Decision(False, (
                "refused: a declared minimum inflow must be greater than zero — "
                "a minimum of nothing asserts nothing"))
        if not intent.inflow_token:
            return Decision(False, (
                "refused: a minimum inflow was declared with no inflow_token to "
                "measure it on"))
        received = _measured_inflow_raw(intent, deltas)
        if received is None:
            return Decision(False, (
                f"refused: the simulation measured NO inflow of the declared "
                f"{intent.inflow_token} — the call spends but returns nothing that "
                f"can be observed, which is a measurement failure, not a free gift"))
        if received < intent.min_inflow_raw:
            return Decision(False, (
                f"refused: the simulation returns {received} of "
                f"{intent.inflow_token}, below the declared minimum "
                f"{intent.min_inflow_raw} — the call does not deliver what it "
                f"promised"))

    # -- 6b. Event-log cross-check ----------------------------------------
    # The reads above cover ONLY the declared token and the measured spenders;
    # a grant to an undeclared spender or a drain of an undeclared token was
    # invisible to them (found in the 2026-08-14 T4 review). The simulated
    # tx's own event log covers whoever it actually touched. Measured pairs
    # are judged by their read delta (authoritative — some tokens re-emit
    # Approval(remaining) on transferFrom, which is a decrease, not a grant);
    # an Approval EVENT on an UNMEASURED pair refuses outright.
    measured_pairs = {(t.lower(), s.lower()) for (t, s) in deltas.allowance_deltas}
    for (l_token, l_spender, l_amount) in deltas.holder_approvals:
        if l_amount <= 0:
            continue                      # setting an allowance to 0 is a revoke
        if (l_token.lower(), l_spender.lower()) in measured_pairs:
            continue                      # the read delta above already judged it
        return Decision(False, (
            f"refused: the transaction emits an UNDECLARED Approval of {l_amount} "
            f"on {l_token} to {l_spender} — a hidden approve is not bounded by a "
            f"USD cap, because the drain happens in a later transaction"))
    for (l_token, _l_to, l_amount) in deltas.holder_transfers:
        if intent.is_liquidity_op and l_token.lower() in liquidity_guard.legs(intent.lp_outflows):
            continue
        if intent.token and l_token.lower() == intent.token.lower():
            continue                      # the declared outflow, asserted above
        if l_amount > 0:
            return Decision(False, (
                f"refused: the transaction emits a Transfer of {l_amount} from the "
                f"wallet on UNDECLARED token {l_token} — it moves an asset the "
                f"intent never mentioned"))

    # -- 6c/6d/6e. NON-FUNGIBLE cross-check (2026-09-15) -------------------
    # ⚠️ These run on EVERY transaction, not only the NFT verbs. Until the
    # simulation learned the 4-topic shapes, `defi_trade.call` / `dapp_connect`
    # could move a collectible out of the treasury and NOTHING above would fire:
    # the reads cover only declared fungibles, and the log parser skipped the
    # event entirely. This is the refusal that closes that.
    _declared_out = {(c.lower(), str(s).lower(), int(i), int(a))
                     for (c, s, i, a) in intent.nft_out}
    for (l_c, l_std, _l_to, l_id, l_amt) in deltas.holder_nft_out:
        if intent.is_liquidity_op and intent.lp_position_effect == "burn":
            continue  # exact burn, including zero recipient, asserted above
        if (l_c.lower(), l_std, int(l_id), int(l_amt)) in _declared_out:
            continue
        return Decision(False, (
            f"refused: the transaction moves an UNDECLARED non-fungible out of "
            f"the wallet — {l_std} {l_c} #{l_id} x{l_amt}. A collectible has no "
            f"price, so no USD cap can bound this; it must be declared"))

    # 6d — a DECLARED move must actually be measured. Broadcasting on a promise
    # is what the 042 min-inflow assertion ended for fungibles.
    _emitted_out = {(c.lower(), s, int(i), int(a))
                    for (c, s, _t, i, a) in deltas.holder_nft_out}
    for (c, s, i, a) in intent.nft_out:
        if (c.lower(), str(s).lower(), int(i), int(a)) not in _emitted_out:
            return Decision(False, (
                f"refused: the intent declares it sends {s} {c} #{i} x{a}, but "
                f"the simulation does not move it — the transaction does not do "
                f"what the intent says"))
    _emitted_in = {(c.lower(), s, int(i), int(a))
                   for (c, s, _t, i, a) in deltas.holder_nft_in}
    for (c, s, i, a) in intent.expected_nft_in:
        if (c.lower(), str(s).lower(), int(i), int(a)) not in _emitted_in:
            return Decision(False, (
                f"refused: the intent declares it receives {s} {c} #{i} x{a}, "
                f"but the simulation shows it does not arrive"))

    # 6e — the blanket grant. A grant can never be declared (step 3 refuses a
    # True in `nft_operator_ops`), so ANY observed grant is undeclared by
    # construction. A revoke only retires a claim and is always fine.
    for (l_c, l_op, l_approved) in deltas.holder_operator_grants:
        if l_approved:
            return Decision(False, (
                f"refused: the transaction emits ApprovalForAll({l_op}, true) on "
                f"{l_c} — a blanket operator approval over the whole collection, "
                f"present and future holdings alike. The drain happens in a "
                f"later transaction no cap here can see"))
    _declared_nft_approvals = {(c.lower(), str(o).lower())
                               for (c, o, _v) in intent.nft_operator_ops}
    for (l_c, l_to, l_id) in deltas.holder_nft_approvals:
        if (intent.is_liquidity_op and intent.lp_position_effect == "burn"
                and l_c.lower() == intent.lp_position[0].lower()
                and l_id == intent.lp_position[1] and l_to.lower() == _ZERO_ADDRESS):
            continue  # ERC721._burn clears this token's approval
        if (l_c.lower(), l_to.lower()) in _declared_nft_approvals:
            continue
        return Decision(False, (
            f"refused: the transaction emits an UNDECLARED Approval of "
            f"{l_c} #{l_id} to {l_to} — a standing claim on that token"))

    # -- 6f. The REGISTRATION receipt (046) -------------------------------
    # (initialised above the branch so every later return can carry it)
    # ⚠️ The whole point of the shape. `register()` transfers no value, so
    # every check above passes for a transaction that did nothing but burn gas.
    # The ERC-8004 Identity Registry is an ERC-721 and `register()` MINTS --
    # `agentId` IS the tokenId -- so a genuine registration always emits
    # Transfer(from=0x0, to=us) from the registry itself. Absence refuses.
    registered_agent_id = None
    if intent.is_registration:
        want = (intent.expected_registry or "").lower()
        minted = [e for e in deltas.holder_nft_in
                  if e[0].lower() == want and e[2].lower() == _ZERO_ADDRESS.lower()]
        if not intent.expects_mint:
            # An UPDATE. The inverse assertion, and the one that matters: a
            # transaction that claims to change the published file while
            # actually minting has created a second identity.
            if minted:
                return Decision(False, (
                    f"refused: this transaction updates the registration file "
                    f"but MINTS agent token {minted[0][3]} -- an update that "
                    f"mints leaves two agentIds and no way to say which "
                    f"identity is authoritative"))
            if any(e[0].lower() == want for e in deltas.holder_nft_in):
                return Decision(False, (
                    "refused: this transaction receives an agent token while "
                    "claiming only to update the registration file"))
        elif not minted:
            from_other = [e for e in deltas.holder_nft_in if e[0].lower() == want]
            if from_other:
                # Somebody TRANSFERRED us an existing agent token. That is
                # someone handing over THEIR identity, not us registering ours,
                # and it is emphatically not what the caller asked for.
                return Decision(False, (
                    f"refused: the registry {intent.expected_registry} transfers "
                    f"an EXISTING agent token to this wallet rather than MINTING "
                    f"a new one -- that is receiving somebody else's identity, "
                    f"not registering"))
            return Decision(False, (
                f"refused: the transaction does not mint an agent token from "
                f"{intent.expected_registry}. A registration that mints nothing "
                f"is a contract call that consumed gas, and nothing above can "
                f"tell the two apart -- which is why this check exists"))
        elif len(minted) > 1:
            return Decision(False, (
                f"refused: the transaction mints {len(minted)} agent tokens; a "
                f"registration mints exactly one identity"))
        if minted and intent.expects_mint:
            # Only a MINT yields an id to report. An update mints nothing, so
            # reporting one would be inventing a fact.
            registered_agent_id = int(minted[0][3])

    # ERC-20 ONLY, as this check's own constant always said: "native balance is
    # not expected to move on an ERC-20 transfer". A NATIVE send declares that
    # move, and the branch above asserts it against the declared amount — running
    # this check on it would refuse every honest native transaction.
    if intent.token and intent.min_native_inflow_wei is not None:
        # A DECLARED native receipt (042): a sell into a native-quoted venue.
        # Asserted, not waved through — the measurement decides.
        if intent.min_native_inflow_wei <= 0:
            return Decision(False, (
                "refused: a declared minimum native inflow must be greater than "
                "zero — a minimum of nothing asserts nothing"))
        if deltas.native_delta <= 0:
            return Decision(False, (
                f"refused: the transaction declared a native RECEIPT but the "
                f"simulation measures a native delta of {deltas.native_delta} "
                f"wei — the sale returns nothing observable"))
        if deltas.native_delta < intent.min_native_inflow_wei:
            return Decision(False, (
                f"refused: the simulation returns {deltas.native_delta} wei, "
                f"below the declared minimum {intent.min_native_inflow_wei} — "
                f"the trade does not deliver what it promised"))
    elif intent.token and abs(deltas.native_delta) > _NATIVE_DUST_WEI:
        return Decision(False, (
            f"refused: unexpected native balance change of {deltas.native_delta} wei — "
            f"the transaction does something that was not declared"))

    # -- 7. Price the risk -------------------------------------------------
    amount_usd = None
    if (intent.is_claim or intent.is_nft_op or intent.is_registration
            or (intent.is_liquidity_op and not intent.lp_outflows)):
        # The cost of a claim is its FEE. Nothing leaves, so pricing an outflow
        # would book it at $0.00 — the confident-zero class that let 114
        # tenantless wallet_spend rows read as a treasury that had spent
        # nothing. The RECEIPT is emphatically not the cost: charging 13.6 ETH
        # arriving against the spend caps would refuse the agent its own money.
        #
        # An NFT op prices the SAME way, for a different reason: a collectible
        # is genuinely unpriceable, and inventing a floor would make the cap
        # lie about the one asset class that is easiest to wash-trade. The fee
        # is the only figure here that is true. What actually bounds an NFT
        # move is the owner (spend_lane.ALWAYS_OWNER_APPROVED_VERBS), not a cap.
        _what = "the claim fee" if intent.is_claim else "the transaction fee"
        # A registration's whole cost is its fee: it sends nothing and receives
        # an identity that has no market price to book against a cap.
        from core.wallet import chains as _chains
        _row = _chains.get(intent.chain)
        _proxy = getattr(_row, "wrapped_native", None) if _row else None
        if not _proxy:
            return Decision(False, (
                f"refused: chain {intent.chain!r} pins no wrapped-native address, "
                f"so {_what} cannot be priced — and an unpriced spend "
                f"booked at $0.00 would widen every other cap by its own size"))
        try:
            unit_price = price_fn(intent.chain, _proxy) if price_fn else None
        except Exception:
            unit_price = None
        if unit_price is None:
            return Decision(False, _unpriceable_reason(
                _what, exit_bounded=False,
                had_fallback=fallback_price_fn is not None))
        _sized_gas = int((deltas.gas_used or 0) * 3 // 2) or int(tx.get("gas") or 0)
        _worst_fee_wei = _sized_gas * int(tx.get("maxFeePerGas") or 0)
        _decimals = int(getattr(_row, "native_decimals", 18) or 18)
        amount_usd = (_worst_fee_wei / (10 ** _decimals)) * unit_price
    elif intent.is_liquidity_op:
        try:
            amount_usd, lp_implied = liquidity_guard.price_outflows(
                intent, deltas, price_fn, fallback_price_fn, _decimals_for)
        except (ValueError, TypeError) as exc:
            return Decision(False, f"refused: {exc}")
    elif intent.is_allowance_op:
        # The risk of an allowance op is the DECLARED GRANT, not the (zero)
        # outflow — pricing the outflow valued every approval at $0.00 and no
        # cap could bound it (§1.1, 2026-08-14).
        # A revoke declares no grant → $0 risk and needs no price at all, so
        # cleaning up a worthless/unpriceable token always stays possible.
        amount_usd, refusal = _price_allowance_grants(
            intent, price_fn=price_fn, fallback_price_fn=fallback_price_fn,
            execution_context=execution_context)
        if refusal is not None:
            return refusal
    elif intent.token:
        try:
            unit_price = price_fn(intent.chain, intent.token) if price_fn else None
        except Exception:
            unit_price = None
        if unit_price is None:
            # 028 (2026-08-22): same exit exemption as the allowance-pricing
            # branch above — an outflow (e.g. a swap's token_in) that does not
            # exceed the wallet's OWN held balance of intent.token puts no NEW
            # value at risk beyond what is already owned; the swap's route
            # check and slippage bound already guard EXECUTION risk
            # separately. Still needs SOME price to compute the caps below.
            exit_bounded = (
                intent.held_balance_raw is not None
                and outflow_raw <= intent.held_balance_raw)
            if exit_bounded and fallback_price_fn:
                try:
                    unit_price = fallback_price_fn(intent.chain, intent.token)
                except Exception:
                    unit_price = None
                if unit_price is not None:
                    logger.info(
                        "tx_guard.exit_exemption chain=%s token=%s outflow=%s "
                        "held_balance=%s — priced via fallback, high-confidence "
                        "price bar waived for an exit within held balance",
                        intent.chain, intent.token, outflow_raw, intent.held_balance_raw)
            if unit_price is None and intent.min_native_inflow_wei is not None:
                # 042: the NATIVE twin of the exit-untying valuation below. A
                # launchpad token minutes old has no price at ANY source, so
                # pricing the outflow dead-ends and the sell refuses — the
                # reachable-but-not-executable failure that left fired stop
                # rules unexecutable in August, reproduced on a new rail.
                #
                # The measured native RECEIPT is exact and unfalsifiable — a
                # better cap number than any estimate of the token — and it was
                # already asserted against the declared minimum in step 6b. An
                # unpriceable native asset still refuses; so does a zero
                # receipt, because unmeasured is never free.
                amount_usd = _native_inflow_valuation_usd(
                    intent, deltas, price_fn=price_fn)
                if amount_usd is not None:
                    logger.info(
                        "tx_guard.native_inflow_valuation chain=%s token_in=%s "
                        "amount_usd=%.4f — outflow unpriceable; caps run "
                        "against the measured native receipt",
                        intent.chain, intent.token, amount_usd)
            if unit_price is None and amount_usd is None and exit_bounded and intent.inflow_token:
                # 2026-08-26 exit untying: the sell of a held token that NO
                # source can price is valued at the simulation's MEASURED
                # inflow of the declared receive token. The receipt is exact
                # and unfalsifiable — a better cap number than any estimate —
                # and refusing here is what left fired stop rules (BPAD,
                # BaseUnc at −54%) permanently unexecutable. A missing or zero
                # measured inflow still refuses: unmeasured is never free.
                amount_usd = _inflow_valuation_usd(
                    intent, deltas, price_fn=price_fn,
                    fallback_price_fn=fallback_price_fn)
                if amount_usd is not None:
                    logger.info(
                        "tx_guard.exit_inflow_valuation chain=%s token_in=%s "
                        "inflow_token=%s amount_usd=%.4f — outflow unpriceable "
                        "by any source; caps run against the measured receipt",
                        intent.chain, intent.token, intent.inflow_token,
                        amount_usd)
            if unit_price is None and amount_usd is None:
                return Decision(False, _unpriceable_reason(
                    "the outflow", exit_bounded=exit_bounded,
                    had_fallback=fallback_price_fn is not None))
        if amount_usd is None:
            decimals = _decimals_for(intent.chain, intent.token)
            if decimals is None:
                return Decision(False, "refused: token decimals unknown — cannot value the outflow")
            amount_usd = (outflow_raw / (10 ** decimals)) * unit_price
    elif intent.is_deploy:
        # A deployment's cost is its ENDOWMENT PLUS ITS FEE, and the fee is
        # usually the whole of it — a token deploy endows nothing and still
        # burns 500k gas. Pricing only the (zero) endowment would book every
        # deployment at $0.00, which is the confident-zero class that let 114
        # tenantless wallet_spend rows read as a treasury that had spent nothing.
        # The worst case is what `size_gas` is about to sign for, so that is what
        # the caps are charged.
        from core.wallet import chains as _chains
        _row = _chains.get(intent.chain)
        _proxy = getattr(_row, "wrapped_native", None) if _row else None
        if not _proxy:
            return Decision(False, (
                f"refused: chain {intent.chain!r} pins no wrapped-native address, "
                f"so a deployment's fee cannot be priced — and an unpriced spend "
                f"booked at $0.00 would widen every other cap by its own size"))
        try:
            unit_price = price_fn(intent.chain, _proxy) if price_fn else None
        except Exception:
            unit_price = None
        if unit_price is None:
            return Decision(False, _unpriceable_reason(
                "the deployment fee", exit_bounded=False,
                had_fallback=fallback_price_fn is not None))
        _sized_gas = int((deltas.gas_used or 0) * 3 // 2) or int(tx.get("gas") or 0)
        _worst_fee_wei = _sized_gas * int(tx.get("maxFeePerGas") or 0)
        _decimals = int(getattr(_row, "native_decimals", 18) or 18)
        amount_usd = ((outflow_raw + _worst_fee_wei) / (10 ** _decimals)) * unit_price
    else:
        # NATIVE outflow (039 B1). The chain's WRAPPED native is the pricing
        # proxy — the same asset at the same number — and it is already pinned in
        # the chain registry, so this adds no new price source and no new address
        # to verify. A chain that pins none cannot be priced here and REFUSES:
        # booking an unpriced outflow at $0.00 would widen every other money
        # verb's remaining cap by the size of this transaction, which is the
        # confident-zero class the 114 tenantless wallet_spend rows came from.
        from core.wallet import chains as _chains
        _row = _chains.get(intent.chain)
        _proxy = getattr(_row, "wrapped_native", None) if _row else None
        if not _proxy:
            return Decision(False, (
                f"refused: chain {intent.chain!r} pins no wrapped-native address, so a "
                f"native outflow cannot be priced — and an unpriced outflow booked at "
                f"$0.00 would widen every other cap by its own size"))
        try:
            unit_price = price_fn(intent.chain, _proxy) if price_fn else None
        except Exception:
            unit_price = None
        if unit_price is None:
            return Decision(False, _unpriceable_reason(
                "the native outflow", exit_bounded=False,
                had_fallback=fallback_price_fn is not None))
        _decimals = int(getattr(_row, "native_decimals", 18) or 18)
        amount_usd = (outflow_raw / (10 ** _decimals)) * unit_price

    # A DECLARED grant is priced whatever shape the transaction is (S4,
    # 2026-09-14). The loop above only ran under `is_allowance_op`, so
    # `defi_trade.call` — which declares `allow_spender`/`allow_max_raw` and is
    # NOT an allowance op — had its grant valued at nothing: the outflow alone
    # carried the cap, and the standing claim the call left behind was free. A
    # claim on funds belongs under the same ceiling as a spend, in every verb
    # that can create one.
    if intent.expected_allowance_grants and not intent.is_allowance_op:
        grants_usd, refusal = _price_allowance_grants(
            intent, price_fn=price_fn, fallback_price_fn=fallback_price_fn,
            execution_context=execution_context)
        if refusal is not None:
            return refusal
        amount_usd = (amount_usd or 0.0) + grants_usd

    if amount_usd is None:
        return Decision(False, "refused: could not compute a USD value for this transaction")
    # Caps operate in CENTS (2026-08-26): a $1.9903-vs-$1.99 refusal is a
    # quote-rounding artifact, not a policy — it cost live runs a refuse/resize/
    # retry dance at every cap edge. Sub-cent drift carries no risk a cap can
    # meaningfully bound.
    if intent.is_liquidity_op and not intent.lp_outflows:
        if amount_usd <= 0:
            return Decision(False, "refused: liquidity fee was not measured/priced")
        amount_usd = max(0.01, amount_usd)
    amount_usd = round(amount_usd, 2)
    if monitor_exit and not intent.is_allowance_op and not intent.is_liquidity_op:
        if not _measured_inflow_raw(intent, deltas):
            return Decision(False, (
                "refused: the monitor-exit lane requires a measured inflow of "
                "the declared receive token — the simulation shows none, so "
                "this is not an exit"))
    if amount_usd > intent.max_spend_usd:
        return Decision(False, (
            f"refused: ${amount_usd:.4f} exceeds the declared max_spend_usd "
            f"${intent.max_spend_usd:.4f}"))

    # -- 8. PolicyGate (per-tx ceiling, rolling caps, replay) --------------
    verdict = gate.check(venue="defi", amount_usd=amount_usd,
                         idempotency_key=intent.idempotency_key)
    if not verdict.allowed:
        from core.security.refusals import record_refusal
        record_refusal("money_gate", tool=getattr(tool_self, "name", None) or "",
                       user_id=getattr(execution_context, "user_id", None) or "",
                       detail=verdict.reason)
        return Decision(False, f"refused by PolicyGate: {verdict.reason}", amount_usd=amount_usd)

    # -- 9. Approval lane --------------------------------------------------
    _ceiling = autonomous_max_usd(*ceiling_scope(execution_context))
    if amount_usd > _ceiling:
        return Decision(False, (
            f"owner approval required: ${amount_usd:.4f} is above the autonomous "
            f"ceiling ${_ceiling:.2f}"),
            lane="owner_queue", amount_usd=amount_usd,
            sim_gas_used=deltas.gas_used, deploy=deploy_facts,
            agent_id=registered_agent_id, position_token_id=position_token_id)

    # An unattended, self-directed spend must have an aggregate damage bound. The
    # per-tx ceiling ($25 default) alone cannot stop an injection during a trade
    # leg from looping within-ceiling swaps that drain the treasury one ticket at
    # a time. WALLET_DAILY_CAP_USD is that bound; H3 (2026-08-22) gave it a
    # finite $100/24h default, so this refusal now only fires when the operator
    # has explicitly disabled the cap (WALLET_DAILY_CAP_USD=none/off) — arming
    # DEFI_AUTONOMOUS_TURN_TRADING on top of that would leave the loop unbounded.
    # Refuse the autonomous lane in that case; an owner-driven turn is unaffected
    # (autonomous_origin is False for a genuine owner turn).
    if autonomous_origin and not getattr(gate, "has_daily_cap", False):
        return Decision(False, (
            "refused: unattended trading needs an aggregate damage bound — set "
            "WALLET_DAILY_CAP_USD (the per-tx ceiling alone can be looped within "
            "to drain the treasury). Owner-driven trades are unaffected."),
            lane="owner_queue", amount_usd=amount_usd,
            sim_gas_used=deltas.gas_used, deploy=deploy_facts,
            agent_id=registered_agent_id, position_token_id=position_token_id)

    return Decision(True, "authorized" + (" (paired-leg implied valuation)" if lp_implied else ""), lane="autonomous", amount_usd=amount_usd,
                    sim_gas_used=deltas.gas_used, deploy=deploy_facts,
                    agent_id=registered_agent_id, position_token_id=position_token_id)


def _price_allowance_grants(intent: TxIntent, *, price_fn, fallback_price_fn,
                            execution_context) -> Tuple[float, Optional[Decision]]:
    """USD at risk in the DECLARED allowance grants — ``(total, None)``, or
    ``(0.0, Decision)`` when it cannot be computed and the transaction refuses.

    Every grant must carry a trustworthy price: a low-confidence (thin/
    seedable-pool) token reads None, and an unpriceable grant that is not an
    exit REFUSES rather than silently skipping the USD bound — the owner can
    still approve it by hand.
    """
    total = 0.0
    for (g_token, g_spender, g_amount) in intent.expected_allowance_grants:
        try:
            unit_price = price_fn(intent.chain, g_token) if price_fn else None
        except Exception:
            unit_price = None
        if unit_price is None:
            # 028 (2026-08-22): a grant that cannot exceed what the wallet
            # ALREADY holds of g_token, to the router the sell leg approves,
            # puts no NEW value at risk — the high-confidence price bar exists
            # to bound a grant that could move more than intended, and an exit
            # within held balance structurally cannot. It is priced by
            # fallback_price_fn (unconstrained by the liquidity/pool-count
            # floor) for the cap checks.
            exit_bounded = _grant_is_exit_bounded(intent, g_token, g_spender,
                                                  g_amount)
            if exit_bounded and fallback_price_fn:
                try:
                    unit_price = fallback_price_fn(intent.chain, g_token)
                except Exception:
                    unit_price = None
                if unit_price is not None:
                    logger.info(
                        "tx_guard.exit_exemption chain=%s token=%s amount=%s "
                        "held_balance=%s — priced via fallback, high-confidence "
                        "price bar waived for an exit within held balance",
                        intent.chain, g_token, g_amount, intent.held_balance_raw)
            if unit_price is None:
                if exit_bounded:
                    # An unvalued grant must not read as FREE (S3, 2026-09-14).
                    # The 2026-08-26 untying valued this case at $0 so a fired
                    # stop rule on an unpriceable coin stayed closable — but $0
                    # also cleared every cap, charged nothing to the daily
                    # budget and ran on the autonomous lane, which is exactly
                    # what an injected approve wanted. Charging the autonomous
                    # ceiling plus a cent keeps the position closable (the
                    # owner is ASKED, not refused) while the claim is finally
                    # bounded by a number.
                    charge = (autonomous_max_usd(*ceiling_scope(execution_context))
                              + _UNVALUED_GRANT_EPSILON_USD)
                    logger.warning(
                        "tx_guard.unvalued_grant chain=%s token=%s amount=%s "
                        "spender=%s held_balance=%s — no price by ANY source; "
                        "charging the autonomous ceiling ($%.2f) so the grant "
                        "goes to the owner queue instead of reading as free",
                        intent.chain, g_token, g_amount, g_spender,
                        intent.held_balance_raw, charge)
                    total += charge
                    continue
                return 0.0, Decision(False, _unpriceable_reason(
                    "the allowance grant", exit_bounded=exit_bounded,
                    had_fallback=fallback_price_fn is not None))
        decimals = _decimals_for(intent.chain, g_token)
        if decimals is None:
            return 0.0, Decision(False, (
                "refused: token decimals unknown — cannot value the allowance grant"))
        total += (g_amount / (10 ** decimals)) * unit_price
    return total, None


def _measured_inflow_raw(intent: TxIntent, deltas) -> Optional[int]:
    """The simulation's measured POSITIVE delta of the declared inflow token."""
    if not intent.inflow_token:
        return None
    want = intent.inflow_token.lower()
    for token, moved in (deltas.token_deltas or {}).items():
        if token.lower() == want and moved > 0:
            return moved
    return None


def _native_inflow_valuation_usd(intent: TxIntent, deltas, *, price_fn):
    """USD of the simulated NATIVE receipt, or None when it cannot be valued (042).

    The chain's WRAPPED native is the pricing proxy — the same asset at the same
    number, already pinned in the registry — so this adds no new price source
    and no new address to verify, exactly as the native-outflow branch does.
    """
    moved = int(getattr(deltas, "native_delta", 0) or 0)
    if moved <= 0:
        return None
    from core.wallet import chains as _chains
    row = _chains.get(intent.chain)
    proxy = getattr(row, "wrapped_native", None) if row else None
    if not proxy:
        return None
    try:
        unit_price = price_fn(intent.chain, proxy) if price_fn else None
    except Exception:
        unit_price = None
    if unit_price is None:
        return None
    decimals = int(getattr(row, "native_decimals", 18) or 18)
    return (moved / (10 ** decimals)) * unit_price


def _inflow_valuation_usd(intent: TxIntent, deltas, *, price_fn,
                          fallback_price_fn) -> Optional[float]:
    """USD value of the measured receipt, or None when it cannot be computed.

    The chain's pinned USDC is $1.00 by definition — it is the unit the whole
    cap system is denominated in, and requiring a price FEED for it is how a
    feed outage once blocked exits outright. Any other inflow token (e.g. WETH
    on a chain with no pinned stablecoin) must be priced by a source.
    """
    inflow_raw = _measured_inflow_raw(intent, deltas)
    if not inflow_raw:
        return None
    decimals = _decimals_for(intent.chain, intent.inflow_token)
    if decimals is None:
        return None
    unit = None
    try:
        from core.wallet import chains
        row = chains.get(intent.chain)
        if row is not None and row.usdc and \
                intent.inflow_token.lower() == row.usdc.lower():
            unit = 1.0
    except Exception:
        unit = None
    if unit is None and price_fn:
        try:
            unit = price_fn(intent.chain, intent.inflow_token)
        except Exception:
            unit = None
    if unit is None and fallback_price_fn:
        try:
            unit = fallback_price_fn(intent.chain, intent.inflow_token)
        except Exception:
            unit = None
    if unit is None:
        return None
    return (inflow_raw / (10 ** decimals)) * unit


def _unpriceable_reason(what: str, *, exit_bounded: bool,
                        had_fallback: bool) -> str:
    """Why this could not be priced, and therefore what the caller can DO.

    029 R6. All three situations used to share one sentence ("no trustworthy
    price"), and the cost of that was concrete: the prod agent wrote a full
    design proposal for a deadlock that had already shipped as 028 and was live
    on the deployed SHA. It could not tell whether the exit exemption had failed,
    had never applied, or did not exist — so it assumed the last one.

    The gate is unchanged. Only the sentence differs, by which lever is missing.
    """
    head = f"refused: {what} has no trustworthy price, so no cap can bound "
    tail = ("what it puts at risk" if "allowance" in what else "it")
    if exit_bounded and had_fallback:
        return (head + tail + ". This IS an exit within your held balance, so the "
                "high-confidence price bar was already waived (028) — but the "
                "fallback pricer returned no price at all, so there is still no "
                "number to run the caps against. That is a price-source outage, "
                "not a policy refusal: retry when pricing recovers, or have the "
                "owner approve it by hand.")
    if exit_bounded and not had_fallback:
        return (head + tail + ". It is an exit within your held balance, which "
                "028 exempts from the high-confidence price bar — but no fallback "
                "pricer was wired into this call, so the exemption could not be "
                "applied. This is a wiring gap, not a decision about your trade.")
    return (head + tail + ". The exit exemption (028) did NOT apply here: it "
            "needs a declared held balance covering the amount, and this call "
            "declared none (or less than the amount), so the grant could put "
            "MORE at risk than you already own. Read your balance and declare it, "
            "or have the owner approve this by hand — an unpriceable token can be "
            "approved by the owner, not autonomously.")


def _decimals_for(chain: str, token: str) -> Optional[int]:
    try:
        from core.wallet.tokens import get_token_identity
        return get_token_identity(chain, token).decimals
    except Exception:
        return None
