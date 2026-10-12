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
import math
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
    #: CR-H07: the raw supply the deployed token's constructor MUST mint to the
    #: holder (``Transfer(0x0 -> holder)`` from the new contract, exact sum).
    #: Set by ``deploy_token``; None = no mint is asserted.
    expected_holder_mint_raw: Optional[int] = None
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
    #: 050 §7.3, 069 v4 §2 — "the key signs, the account holds". The NFT's ERC-6551
    #: account, operated ONLY as the NFT's current owner: the pre-flight refuses unless
    #: ``owner()`` of the account IS the signing treasury (no grants, no operator keys).
    #: When set, the signed transaction is ``{to: via_account, data:
    #: execute(inner_to, value, inner_data, 0)}`` and EVERY other field of this
    #: intent describes the INNER call; the guard measures the account as the
    #: holder and the treasury as the sender. Not a seventh shape: every shape
    #: except deploy and liquidity may ride it.
    via_account: Optional[str] = None
    #: The account's ``state()`` the verb read when it built this intent. A
    #: pre-flight re-read that differs refuses (the account changed under us —
    #: a sale, a lock, a nested execute). Required with ``via_account``.
    via_account_state: Optional[int] = None
    #: W3 — single-token ERC-721 approvals this call CLEARS: ``(contract, token_id)``, the
    #: ``approve(address(0), id)`` shape (``erc6551.encode_erc721_revoke``). Only with
    #: ``is_nft_op``. Held to the simulation: ``Approval(holder, 0x0, id)`` from that
    #: contract must be emitted, or the call refuses.
    nft_approval_revokes: Sequence[Tuple[str, int]] = ()
    #: C17 — ERC-6909 per-id allowances this call REVOKES: ``(contract, spender, id)``, the
    #: ``approve(spender, id, 0)`` shape (``erc6551.encode_erc6909_revoke``). Only with
    #: ``is_nft_op``. Held to the simulation: ``Approval(holder, spender, id, 0)`` from that
    #: contract must be emitted. (An ERC-6909 operator revoke is ``nft_operator_ops`` with False.)
    erc6909_revokes: Sequence[Tuple[str, str, int]] = ()
    #: Owner-accepted open-approval rows (``erc6551.approval_key``) that rule 5 may ignore on a
    #: pinned NFT's departure — ONLY rows no transaction an NFT owner sent emitted (possibly
    #: fabricated by any contract). A row an owner's transaction granted always blocks.
    accepted_unattributed_approvals: Sequence[str] = ()
    #: W3 — Permit2 allowances this call REVOKES: ``(token, spender)``, via Permit2
    #: ``approve(token, spender, 0, 0)`` or ``lockdown``. Only with ``is_allowance_op`` (no
    #: grants), ``to`` = the pinned Permit2 and ``token`` = one of the revoked tokens. Held to
    #: the simulation: Permit2 must emit ``Approval``/``Lockdown`` with amount 0 for each pair.
    permit2_revokes: Sequence[Tuple[str, str]] = ()
    #: W8 — the approve-spend-reset batch through a token-bound account (requires ``via_account``).
    #: The owner signs ``account.executeBatch([L1, L2, L3])`` with EXACTLY three operation-0
    #: legs: L1 = ``token.approve(S, X)`` (0 < X <= ``amount_raw``), L2 = the spend call to
    #: ``S`` = ``to``, L3 = ``token.approve(S, 0)``. Every leg is checked structurally; L2 is
    #: the inner call the rest of the guard judges (outflow of ``token`` <= ``amount_raw``);
    #: and the simulation must show ``allowance(account, S) == 0`` AFTER — no claim survives
    #: the transaction. Any other ``executeBatch`` stays an account-admin call and refuses.
    via_account_batch: bool = False
    #: J1 — the account's journal entry rides the SAME ``executeBatch`` as the action: the LAST
    #: leg is ``JournalLog.log(entry)`` (requires ``via_account``). The guard allows exactly this
    #: leg — target = the ``journal_log`` pinned for the account's collection, selector
    #: ``log(bytes)``, value 0, operation 0, and the bytes one canonical signed journal entry of
    #: THIS account and chain, signed by the signing treasury — then judges the remaining legs as
    #: if the batch were the plain ``execute`` (or the W8 three-leg batch) without it.
    via_account_journal: bool = False
    #: J1 — a journal entry with NO action: the account's single call is ``JournalLog.log(entry)``
    #: (``to`` = the pinned ``journal_log`` of the account's collection, value 0, ``amount_raw``
    #: 0, ``token`` None; requires ``via_account``). The same entry checks as the batch leg, and
    #: the simulation must show nothing of the account move and the ``Entry`` event emitted.
    is_journal_entry: bool = False
    #: The collection reveal (069 v4; a pinned collection profile with the ``reveal``
    #: capability): ``reveal(uint256[] ids)`` on a pinned collection, signed by the
    #: treasury, value 0. The
    #: registration precedent: it moves nothing, so the RECEIPT is the shape — the simulation
    #: must show exactly one ``Revealed``/``Recommitted`` per id in ``reveal_ids``, every log
    #: from the collection, and no movement of the signer (``core.wallet.collection_reveal``). Its
    #: cost is the fee, priced like a registration's.
    is_collection_reveal: bool = False
    reveal_ids: Sequence[int] = ()
    #: The collection paid mint (069 v4; a pinned collection profile with the ``mint``
    #: capability; testnet-run F2): ``mint(to, qty, minPnlOutPerToken)`` on a pinned collection,
    #: signed by the treasury with ``to`` = the treasury and ``value == amount_raw == qty × PRICE``. The
    #: one shape where native value LEAVES and NFTs ARRIVE: the NFT branch refuses any native
    #: movement and the native branch cannot see a token arrive, so the mint verb could
    #: never pass either. The simulation must show the exact payment and exactly the declared ids
    #: minted to the signer (``core.wallet.collection_mint``). Priced as value + worst-case fee.
    is_collection_mint: bool = False
    #: The ids the mint creates, consecutive (``nextId .. nextId+qty-1``); ``qty`` is their
    #: count and must equal the calldata's ``qty``.
    mint_ids: Sequence[int] = ()


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
    #: Preview balance changes, carried only after authorization. These are
    #: estimates for a confirmed swap's position book, never receipt proof.
    simulated_token_deltas: Optional[dict] = None
    #: Which side supplied the USD estimate; inflow is a proceeds estimate.
    valuation_basis: str = "outflow"
    #: The refusal is a spend-CAP refusal (the PolicyGate's structured flag) — the
    #: signer's above-cap approval path keys on this, never on the reason text.
    cap_exceeded: bool = False


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
        # The IDENTITY axis has ONE home per process (C10, 2026-09-15): every
        # pref WRITER resolves the data home. Reading via ``polyrob_home()``
        # (the unit's $HOME/.polyrob on prod) found an empty store, so an
        # owner-approved ceiling silently fell back to the env value.
        from core.runtime_paths import prefs_home_dir
        return user_id, prefs_home_dir()
    except Exception:
        logger.debug("ceiling scope unresolved; env value applies", exc_info=True)
        return None, None


def autonomous_max_usd(user_id=None, home_dir=None) -> float:
    """Spend that may execute without asking the owner.

    Distinct from the catastrophic per-transaction ceiling, which is a
    loss limit: its env value is the default, and an owner-approved
    ``budget.wallet_per_tx_usd`` pref may LOWER or RAISE it, clamped to the
    daily cap (``core.wallet.config``, since 2026-09-18). This one is an
    interruption dial: raising it cannot widen maximum loss, because the
    backstop still binds, so the owner may raise it from any seat
    (``budget.defi_autonomous_usd``). It is CLAMPED to the backstop here —
    an autonomous ceiling above the per-tx ceiling would be a number that can
    never be reached, and reporting it would mislead.

    Fail-open to the env value: an unreadable pref store must never widen
    spend authority, and it must not break the guard either.
    """
    from core.config_policy.spend_lane import DEFAULT_DEFI_AUTONOMOUS_MAX_USD
    env_value = float_env("DEFI_AUTONOMOUS_MAX_USD", DEFAULT_DEFI_AUTONOMOUS_MAX_USD)
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
    # M09: a NaN/inf pref (hand-edited file, or written before _coerce refused
    # it) must never reach the comparison — `x > NaN` is always False, so a NaN
    # ceiling waves every spend through. Fall back to the env value.
    if not math.isfinite(value):
        logger.warning("budget.defi_autonomous_usd is not finite (%r); using env", value)
        value = env_value
    try:
        from core.wallet.config import effective_max_per_tx_usd
        backstop = float(effective_max_per_tx_usd(user_id, home_dir))
    except Exception:
        backstop = math.nan
    if not math.isfinite(backstop):
        # The resolver failed: the env default, still under the signer's cap
        # (one default, core.wallet.config) — never wider than the envelope.
        from core.wallet.config import DEFAULT_MAX_PER_TX_USD
        from core.wallet.signer_envelope import clamp
        backstop = float(clamp(float_env("AGENT_WALLET_MAX_PER_TX_USD",
                                         DEFAULT_MAX_PER_TX_USD), "per_tx_usd"))
    return min(value, backstop)


def rpc_is_pinned(chain: str) -> bool:
    """True when the operator has pinned a real endpoint for *chain*.

    Money paths refuse to arm on the shared public default: it is
    unauthenticated and rate-limited, and it is simultaneously the source of the
    simulation, the deltas and the balances the caps are computed from.
    """
    from core.wallet.chains import rpc_env_name
    return bool(os.getenv(rpc_env_name(chain), "").strip())


def _rpc_env_name(chain: str) -> str:
    from core.wallet.chains import rpc_env_name
    return rpc_env_name(chain)


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


def _is_nft_revoke_only(intent: TxIntent) -> bool:
    """An NFT op that only RETIRES approvals (ApprovalForAll false, single-token clears)."""
    return bool(intent.is_nft_op and not intent.nft_out and not intent.expected_nft_in
                and (intent.nft_operator_ops or intent.nft_approval_revokes
                     or intent.erc6909_revokes)
                and not any(bool(a) for (_c, _o, a) in intent.nft_operator_ops))


def intent_is_risk_reducing(intent: TxIntent) -> bool:
    """068 R3-2: withdrawing capital the wallet already has a claim on — LP
    remove/collect (a liquidity op with no outflows), a claim of what is owed,
    a revoke (an allowance op with no grants). Such an intent acquires nothing
    the run's ``target_token`` restricts, so the inflow backstop exempts it."""
    return bool((intent.is_liquidity_op and not intent.lp_outflows)
                or intent.is_claim
                or (intent.is_allowance_op and not intent.expected_allowance_grants)
                or _is_nft_revoke_only(intent))


def _intent_is_exit_shaped(intent: TxIntent) -> bool:
    """True when the DECLARED intent can only CLOSE a position, never open one.

    - a revoke (allowance op, no grants) is always an exit;
    - an approve whose every grant is exit-bounded (``_grant_is_exit_bounded``:
      held token, within held balance, to the declared destination, which is a
      route spender the chain pins);
    - a swap that sends the held token (within held balance) and declares the
      chain's QUOTE ASSET as its inflow is a sell-to-quote.
    The declaration is not trusted on its own: the simulation still asserts the
    deltas, and the sell case additionally requires a measured quote inflow.
    """
    if intent.is_liquidity_op:
        return (not intent.lp_outflows and bool(intent.lp_inflows)
                and any(n > 0 for _, n in intent.lp_inflows))
    if _is_nft_revoke_only(intent):
        return True  # an NFT revoke, like an allowance revoke, only retires a claim
    if intent.is_allowance_op:
        if not intent.expected_allowance_grants:
            return True  # revoke
        # CR-H01: the SAME three conditions the pricing exemption demands
        # (held token within held balance, spender == declared destination,
        # spender pinned as a route spender). A grant of the held token to an
        # arbitrary spender opens a claim on the wallet — it is not an exit,
        # and must not pass the entry-pause or the monitor-exit lane.
        return all(
            _grant_is_exit_bounded(intent, g_token, g_spender, g_amount)
            for (g_token, g_spender, g_amount) in intent.expected_allowance_grants)
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


#: ``execution_context.metadata`` key the controller's approval hook stamps when
#: the owner approved THIS call (``tools/controller/approval.py``). The
#: controller clears it after the call, so it never outlives the action it was
#: minted for.
OWNER_GRANT_KEY = "owner_grant"


def _owner_grant_covers(execution_context, intent) -> bool:
    """True when the owner approved this very call and that approval covered
    this intent's declared bound. Fail-closed: any doubt answers False."""
    try:
        metadata = getattr(execution_context, "metadata", None) or {}
        grant = metadata.get(OWNER_GRANT_KEY)
        if not isinstance(grant, dict) or grant.get("approved") is not True:
            return False
        granted = grant.get("max_spend_usd")
        if isinstance(granted, bool) or not isinstance(granted, (int, float)):
            return False
        return float(intent.max_spend_usd) <= float(granted)
    except Exception:
        return False


def owner_authority(execution_context, forged_fn, tool_self, intent) -> Tuple[bool, bool]:
    """``(owner_direct, owner_granted)`` — step 0, for the rails that mirror
    this guard without an EVM transaction (the Solana swap and send). ONE
    answer, so the mirrors cannot drift from :func:`_authorize`."""
    from core.money.authority import owner_direct_turn
    owner_direct = owner_direct_turn(execution_context, forged_fn, tool_self)
    return owner_direct, (not owner_direct) and _owner_grant_covers(execution_context, intent)


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
              liquidity_rpc: Optional[Callable] = None,
              account_rpc: Optional[Callable] = None) -> Decision:
    """Authorize *tx* against *intent*. Returns a Decision; never broadcasts.

    The caller must hold ``gate.reserve()`` across authorize → broadcast →
    record so a concurrent transaction cannot both clear a nearly-exhausted cap.

    066 P2: with ``WALLET_SIGNER`` = ``shadow``/``remote`` an allowed decision
    (or the owner-approval lane) is recorded in :mod:`core.signer.attest`, so
    the signing point can hand the SAME intent to ``polyrob-signer``, which runs
    this very function again in its own process. ``local`` records nothing.
    """
    decision = _authorize(
        intent, tx, holder=holder, gate=gate, execution_context=execution_context,
        tool_self=tool_self, simulate_fn=simulate_fn, price_fn=price_fn,
        fallback_price_fn=fallback_price_fn, rpc_is_pinned_fn=rpc_is_pinned_fn,
        halted_fn=halted_fn, entry_paused_fn=entry_paused_fn, forged_fn=forged_fn,
        autonomous_ok_fn=autonomous_ok_fn, liquidity_rpc=liquidity_rpc,
        account_rpc=account_rpc)
    if decision.allowed and decision.lane in ("owner_direct", "owner_approved"):
        # Book the spend with the lane it was authorized on, so the pause alarm
        # can tell the owner acting from the agent breaching (fail-open: a fake
        # gate without the note is only a less precise audit row).
        try:
            gate.note_lane(intent.idempotency_key, decision.lane)
        except Exception:
            logger.debug("spend lane note skipped", exc_info=True)
    if decision.allowed or decision.lane == "owner_queue":
        try:
            from core.signer import MODE_LOCAL, signer_mode
            if signer_mode() != MODE_LOCAL:
                from core.signer import attest
                attest.record(intent, tx)
        except Exception:
            logger.warning("signer attestation not recorded; the signer will refuse this send",
                           exc_info=True)
    return decision


def _principal_refusal(execution_context) -> Optional[str]:
    """Step 2's principal check (``core.money.authority.turn_refusal``), kept in
    this module so the money-kernel ratchet still sees its one allowlisted call
    here, not in the step module that runs it."""
    from core.wallet.authority import turn_refusal
    return turn_refusal(execution_context)


def _simulated_target_refusal(execution_context, intent: TxIntent, deltas,
                              holder: str) -> Optional[str]:
    """Step 5's 068 N1 backstop: the refusal text, or None. Fail-closed — a check
    that cannot run refuses. Kept here, beside the intent predicates it reads."""
    try:
        from core.wallet.buy_target import simulated_acquisition_refusal
        return simulated_acquisition_refusal(
            execution_context, chain=intent.chain, deltas=deltas, holder=holder,
            risk_reducing=intent_is_risk_reducing(intent))
    except Exception as exc:
        return f"refused: the target check could not run ({exc}); failing closed"


def _authorize(intent: TxIntent, tx: dict, *, holder: str, gate,
               execution_context=None, tool_self=None,
               simulate_fn: Optional[Callable] = None,
               price_fn: Optional[Callable] = None,
               fallback_price_fn: Optional[Callable] = None,
               rpc_is_pinned_fn: Optional[Callable] = None,
               halted_fn: Optional[Callable] = None,
               entry_paused_fn: Optional[Callable] = None,
               forged_fn: Optional[Callable] = None,
               autonomous_ok_fn: Optional[Callable] = None,
               liquidity_rpc: Optional[Callable] = None,
               account_rpc: Optional[Callable] = None) -> Decision:
    """The guard itself (see :func:`authorize`).

    W6 (2026-10-10): the numbered steps are functions in
    :mod:`core.wallet.tx_guard_steps`, run in order over one
    :class:`~core.wallet.tx_guard_steps.GuardContext`; the first step that
    returns a Decision answers. Same order, same fail-closed behaviour — a step
    raises exactly where this function used to.
    """
    # getattr: the pause refusal (step 1) must not depend on the intent's shape.
    price_fn = _valueless_price_fn(getattr(intent, "chain", None), price_fn)
    simulate_fn = simulate_fn or (lambda **kw: simulation.simulate(**kw))
    rpc_is_pinned_fn = rpc_is_pinned_fn or rpc_is_pinned
    halted_fn = halted_fn or _halted
    entry_paused_fn = entry_paused_fn or _entry_paused

    from core.wallet.tx_guard_steps import GuardContext, run
    return run(GuardContext(
        intent=intent, tx=tx, holder=holder, gate=gate,
        execution_context=execution_context, tool_self=tool_self,
        simulate_fn=simulate_fn, price_fn=price_fn, fallback_price_fn=fallback_price_fn,
        rpc_is_pinned_fn=rpc_is_pinned_fn, halted_fn=halted_fn,
        entry_paused_fn=entry_paused_fn, forged_fn=forged_fn,
        autonomous_ok_fn=autonomous_ok_fn, liquidity_rpc=liquidity_rpc,
        account_rpc=account_rpc))


# ==========================================================================
# 050 §7.3 — token-bound account (ERC-6551) helpers
# ==========================================================================

def _via_account_inner(intent: TxIntent, tx: dict) -> Tuple[Optional[dict], Optional[str]]:
    """Decode the signed ``execute`` into the INNER call the rest of the guard judges.

    Returns ``(inner_tx, None)`` or ``(None, reason)``. Everything here is
    structural (no RPC): the signed destination IS the account, the calldata IS
    ``execute(inner_to, value, data, 0)``, and the inner call is not aimed back
    at the account itself.
    """
    from core.wallet import erc6551
    via = str(intent.via_account).lower()
    if intent.is_deploy or intent.is_liquidity_op:
        return None, ("a token-bound account cannot deploy or change liquidity (v1): "
                      "`execute` with operation != 0 is an account-admin call, and LP is "
                      "out of scope")
    if intent.via_account_state is None:
        return None, ("an account call must declare the account `state()` it read "
                      "(`via_account_state`) — without it a sale, lock or nested "
                      "execute between build and broadcast is invisible")
    if str((tx or {}).get("to") or "").lower() != via:
        return None, (f"the transaction is addressed to {tx.get('to')!r}, not the "
                      f"declared token-bound account {intent.via_account}")
    if int((tx or {}).get("value") or 0) != 0:
        return None, ("the owner must send no value to the token-bound account — the "
                      "value the inner call moves comes from the account")
    if intent.via_account_batch:
        return _via_account_batch_inner(intent, tx)
    admin = erc6551.admin_selector_name(tx.get("data"))
    if admin:
        return None, (f"`{admin}` on a token-bound account is an account-admin call; the "
                      f"owner may only `execute` with operation 0")
    try:
        inner_to, value, inner_data, op = erc6551.decode_execute(tx.get("data") or "")
    except Exception as exc:  # noqa: BLE001
        return None, f"the account call is not execute(address,uint256,bytes,uint8) ({exc})"
    if op != 0:
        return None, f"execute with operation {op} is an account-admin call"
    if inner_to.lower() == via:
        return None, "the token-bound account may not call itself (that is how admin calls hide)"
    if erc6551.is_trusted_forwarder(inner_to):
        return None, (f"the inner call targets {inner_to}, an ERC-2771 forwarder the account "
                      f"trusts — through it the account would act as its own trusted sender")
    nested = erc6551.embedded_account_selector(inner_data)
    if nested:
        return None, (f"the inner call carries an account-admin or nested execute selector "
                      f"(`{nested}`) — at any depth, an account call may not drive an account")
    inner = {"to": inner_to, "data": inner_data, "value": value}
    for k in ("chainId", "nonce", "gas", "maxFeePerGas", "maxPriorityFeePerGas"):
        if k in tx:
            inner[k] = tx[k]
    return inner, None


def _via_account_batch_inner(intent: TxIntent, tx: dict) -> Tuple[Optional[dict], Optional[str]]:
    """W8: ``executeBatch([approve(S, X), spend, approve(S, 0)])`` — judge each leg; return the
    SPEND leg as the inner call. Structural only (the final allowance is asserted after the
    simulation, in :func:`_authorize`)."""
    from core.wallet import abi, erc6551
    via = str(intent.via_account).lower()
    if (not intent.token or intent.is_allowance_op or intent.is_nft_op or intent.is_claim
            or intent.is_registration or intent.nft_approval_revokes or intent.permit2_revokes
            or intent.expected_allowance_grants):
        return None, ("an approve-spend-reset batch is a token SPEND: declare `token` and no "
                      "other shape (no allowance op, NFT, claim, registration or grant — the "
                      "transient grant is reset inside the batch)")
    try:
        legs = erc6551.decode_execute_batch(tx.get("data") or "")
    except Exception as exc:  # noqa: BLE001
        return None, f"the account batch is not executeBatch((address,uint256,bytes,uint8)[]) ({exc})"
    if len(legs) != 3:
        return None, (f"an approve-spend-reset batch has exactly 3 legs, not {len(legs)} — any "
                      f"other executeBatch is an account-admin call")
    token = intent.token.lower()
    spender = str(intent.to or "").lower()
    approve_sel = abi.selector("approve(address,uint256)")
    for i, (to, value, data, op) in enumerate(legs, 1):
        if op != 0:
            return None, f"batch leg {i} has operation {op} — an account-admin call"
        if to.lower() == via:
            return None, f"batch leg {i} calls the token-bound account itself"
        if erc6551.is_trusted_forwarder(to):
            return None, f"batch leg {i} targets {to}, an ERC-2771 forwarder the account trusts"
        nested = erc6551.embedded_account_selector(data)
        if nested:
            return None, (f"batch leg {i} carries an account-admin or nested execute selector "
                          f"(`{nested}`)")

    def _approve(leg) -> Optional[Tuple[str, int]]:
        to, value, data, _op = leg
        if to.lower() != token or value != 0 or (data or "")[:10].lower() != approve_sel:
            return None
        try:
            s_, amt = abi.decode([{"type": "address"}, {"type": "uint256"}], data[10:])
        except Exception:  # noqa: BLE001
            return None
        return str(s_).lower(), int(amt)

    first, last = _approve(legs[0]), _approve(legs[2])
    if first is None or last is None:
        return None, ("an approve-spend-reset batch must open with token.approve(spender, X) "
                      "and close with token.approve(spender, 0) on the declared token")
    if first[0] != spender or last[0] != spender:
        return None, (f"the batch approves {first[0]} / resets {last[0]}, but the spend leg's "
                      f"declared destination is {intent.to} — the approved spender, the "
                      f"reset spender and the called contract must be the same address")
    if not 0 < first[1] <= int(intent.amount_raw):
        return None, (f"the batch's transient grant {first[1]} must be > 0 and no larger than "
                      f"the declared spend {intent.amount_raw}")
    if last[1] != 0:
        return None, f"the batch's last leg approves {last[1]}, not 0 — it does not reset"
    s_to, s_value, s_data, _ = legs[1]
    if s_to.lower() != spender:
        return None, (f"the batch's spend leg calls {s_to}, not the approved spender "
                      f"{intent.to}")
    inner = {"to": s_to, "data": s_data, "value": s_value}
    for k in ("chainId", "nonce", "gas", "maxFeePerGas", "maxPriorityFeePerGas"):
        if k in tx:
            inner[k] = tx[k]
    return inner, None


def _account_call_refusal(tx: dict, *, deep: bool = True) -> Optional[str]:
    """A call that does NOT declare ``via_account`` must not carry an ERC-6551
    execute or account-admin selector: otherwise caller-supplied calldata could
    drive a token-bound account while the guard measures the TREASURY (whose balances
    do not move), and every account rule would be skipped.

    G0.2 (measured on a 4663 fork, 2026-09-29): checking only the TOP-level
    selector let ``forwarder.aggregate3([(account, execute(...))])`` through as
    a 1-wei treasury send, and 0.05 ETH left the account. So: the destination
    may not be a pinned ERC-2771 forwarder, and with ``deep`` the selector is
    refused at ANY byte offset (a deployment's init code is exempt — bytecode
    carries PUSH4 selectors, and a constructor never acts through an account)."""
    from core.wallet import erc6551
    to = (tx or {}).get("to")
    if erc6551.is_trusted_forwarder(to):
        return (f"the transaction targets {to}, an ERC-2771 forwarder token-bound "
                f"accounts trust — through it the signer acts on an account while the "
                f"guard measures the treasury; an account call must declare `via_account`")
    data = (tx or {}).get("data") or ""
    if not isinstance(data, str) or len(data) < 10:
        return None
    sel = data[:10].lower()
    name = erc6551.ACCOUNT_CALL_SELECTORS.get(sel)
    if name is None and deep:
        name = erc6551.embedded_account_selector(data)
    if name:
        return (f"this calldata carries a token-bound-account call (`{name}`, execute / "
                f"account admin, at any depth); an account call must declare `via_account` "
                f"so the account's assets are what the guard measures")
    return None


def _via_account_preflight(intent: TxIntent, sender: str, rpc, *,
                           inner_to: Optional[str] = None,
                           journal_to: Optional[str] = None) -> Optional[str]:
    """Rule 5: reads BEFORE simulation. Any failed read refuses (fail closed).

    069 v4 (the simple model): the SIGNER must be the NFT's current owner — the only way this
    instance acts through an account. There is no grant path: ``setPermissions`` is an
    account-admin selector the guard refuses, and a permissioned caller is never accepted."""
    from core.wallet import erc6551
    acct = intent.via_account
    try:
        erc6551.verify(rpc)
        impl = erc6551.read_implementation(rpc, acct)
        if impl.lower() != erc6551.ACCOUNT_V3_IMPL.lower():
            return (f"the token-bound account clones {impl}, not the pinned AccountV3 "
                    f"{erc6551.ACCOUNT_V3_IMPL}")
        _coll_why = _account_collection_refusal(intent.chain, acct, rpc, journal_to=journal_to)
        if _coll_why:
            return _coll_why
        if erc6551.read_is_locked(rpc, acct):
            return "the token-bound account is LOCKED by its owner — nothing moves until the lock ends"
        owner = erc6551.read_owner(rpc, acct)
        if str(owner).lower() != str(sender).lower():
            return (f"this wallet ({sender}) does not own the NFT of the token-bound account "
                    f"{acct} (its owner is {owner}) — an agent acts through an account only "
                    f"as the NFT's current owner")
        if inner_to and erc6551.read_is_trusted_forwarder(rpc, acct, inner_to):
            return (f"the token-bound account trusts {inner_to} as an ERC-2771 forwarder — the "
                    f"inner call may not go through it")
        state = erc6551.read_state(rpc, acct)
        if state != int(intent.via_account_state):
            return (f"the token-bound account state moved ({intent.via_account_state} → "
                    f"{state}) since the intent was built — re-read and rebuild")
    except Exception as exc:  # noqa: BLE001
        return f"Account pre-flight could not be verified ({exc}); failing closed"
    return None


def _treasury_side_refusal(intent: TxIntent, deltas, *, holder: str, sender: str) -> Optional[str]:
    """C6: an account call moves the ACCOUNT's assets — the guard measures the account, so a
    movement on the treasury (signer) side would pass every cap unseen. Refuse any of it, with ONE
    exception: the account paying its owner, i.e. the declared destination IS the treasury and
    the only treasury-side events are fungible/NFT ``Transfer(account → treasury)``. A treasury
    log the guard cannot attribute (no raw logs measured) refuses."""
    from core.wallet import simulation
    paid_owner = str(intent.to or "").lower() == str(sender).lower()
    if deltas.sender_native_delta < 0 or (deltas.sender_native_delta > 0 and not paid_owner):
        return (f"the account call changes the treasury's native balance by "
                f"{deltas.sender_native_delta} wei — an account call moves only the account's "
                f"assets")
    if not deltas.sender_moved:
        return None
    word = "0x" + "0" * 24 + str(sender).lower().removeprefix("0x")
    acct = "0x" + "0" * 24 + str(holder).lower().removeprefix("0x")
    seen = False
    for log in deltas.logs or ():
        topics = [str(t).lower() for t in (log.get("topics") or [])]
        if word not in topics[1:]:
            continue
        seen = True
        if not (paid_owner and len(topics) >= 3 and topics[0] == simulation._TOPIC_TRANSFER
                and topics[1] == acct and topics[2] == word and word not in topics[3:]):
            return (f"the account call moves something on the treasury side "
                    f"({log.get('address')} {topics[0][:10]}) — an account call may only pay "
                    f"its owner, as the declared destination")
    if not seen:
        return (f"the account call touches the treasury ({', '.join(deltas.sender_moved)}) and "
                f"the guard cannot tell in which direction; failing closed")
    return None


def _valueless_price_fn(chain: Optional[str], price_fn: Optional[Callable]) -> Optional[Callable]:
    """C13: on a ``valueless`` chain row (a testnet) the native coin and its wrapped form are worth
    exactly $0 — no source prices them, and an unpriced spend refuses. Any other token keeps the
    caller's price (none, on a testnet, so it refuses as unpriceable)."""
    if not chain:
        return price_fn
    from core.wallet import chains
    row = chains.get(chain)
    if row is None or not row.valueless:
        return price_fn
    native = {str(row.wrapped_native or "").lower(), ""}

    def priced(c, addr):
        if str(addr or "").lower() in native:
            return 0.0
        return price_fn(c, addr) if price_fn else None
    return priced


def _account_collection_refusal(chain: str, account: str, rpc, *,
                                journal_to: Optional[str] = None) -> Optional[str]:
    """C4: the account's own ``token()`` must name a PINNED collection on this chain whose live
    code still hashes to its pin, and the account must be that token's canonical account (one
    of the profile's account versions, pure CREATE2). The signer re-runs this guard, so an
    AccountV3 clone of an unpinned or changed collection refuses there too. Fail closed."""
    from core.wallet import collection_registry, erc6551
    chain_id, collection, token_id = erc6551.read_token(rpc, account)
    want = collection_registry.chain_id_of(chain)
    if int(chain_id) != int(want):
        return (f"the token-bound account {account} is bound to a token on chain {chain_id}, "
                f"not {chain} ({want})")
    why = _collection_runtime_refusal(chain, collection, rpc)
    if why:
        return f"the token-bound account {account} belongs to {collection} #{token_id}: {why}"
    profile = collection_registry.profile_for(want, collection)
    canonical = {erc6551.account_address(want, profile.address, int(token_id), salt=v.salt,
                                         implementation=v.implementation).lower()
                 for v in profile.accounts}
    if str(account).lower() not in canonical:
        return (f"{account} is not the pinned ERC-6551 account of {collection} #{token_id} — "
                f"an account outside the profile's account versions")
    if journal_to is not None and str(journal_to).lower() != str(profile.journal_log or ""):
        return (f"the journal leg calls {journal_to}, not the JournalLog pinned for {collection} "
                f"({profile.journal_log or 'none'})")
    return None


def _journal_entry_refusal(intent: TxIntent, data: str, *, signer: str, chain_id: int,
                           prefixes) -> Optional[str]:
    """Why ``log(entry)`` calldata *data* is NOT one canonical journal entry of this account and
    chain signed by *signer* under one of *prefixes* — or None."""
    from core.wallet import journal_log
    from core.wallet.nft_account import recover_owner
    try:
        entry = journal_log.parse_entry(journal_log.decode_log(data))
    except journal_log.JournalLogError as exc:
        return f"the journal leg is not one canonical journal entry ({exc})"
    if (str(entry["account"]).lower() != str(intent.via_account).lower()
            or entry["chain_id"] != chain_id
            or str(entry["owner"]).lower() != str(signer).lower()):
        return ("the journal entry names another account, chain or owner than this account call "
                "and its signer")
    if not any(recover_owner(entry, pfx) == str(signer).lower() for pfx in prefixes):
        return "the journal entry's signature does not recover to the signing treasury"
    return None


def _journal_only_refusal(intent: TxIntent, inner: dict, *, signer: str
                          ) -> Tuple[Optional[str], Optional[str]]:
    """``(journal target, None)`` for a well-formed journal-only account call, else
    ``(None, reason)`` (``TxIntent.is_journal_entry``)."""
    from core.wallet import collection_registry
    if (intent.via_account_batch or intent.via_account_journal or intent.token is not None
            or intent.amount_raw != 0 or intent.is_nft_op or intent.is_allowance_op
            or intent.is_claim or intent.is_registration or intent.expected_allowance_grants
            or intent.inflow_token or intent.min_native_inflow_wei):
        return None, ("a journal entry moves nothing: declare token=None, amount_raw=0 and no "
                      "other shape")
    to = str(inner.get("to") or "").lower()
    if to != str(intent.to or "").lower() or int(inner.get("value") or 0) != 0:
        return None, "the journal entry's call must go to the declared JournalLog with no value"
    try:
        chain_id = collection_registry.chain_id_of(intent.chain)
        prefixes = {p.journal_prefix for p in collection_registry.profiles_on(chain_id)
                    if p.journal_log == to}
    except Exception as exc:  # noqa: BLE001
        return None, f"the owner's collection registry cannot be trusted ({exc}); failing closed"
    if not prefixes:
        return None, f"{inner.get('to')} is no pinned JournalLog on {intent.chain}"
    why = _journal_entry_refusal(intent, inner.get("data") or "", signer=signer,
                                 chain_id=chain_id, prefixes=prefixes)
    return (None, why) if why else (to, None)


def _strip_journal_leg(intent: TxIntent, tx: dict, *, signer: str
                       ) -> Tuple[Optional[dict], Optional[str], Optional[str]]:
    """J1: check the LAST leg of the account's ``executeBatch`` is exactly one journal entry and
    return ``(tx without it, journal target, None)`` — re-encoded as the plain ``execute`` (or the
    W8 three-leg batch) the rest of the guard judges — or ``(None, None, reason)``. Structural
    plus the entry's signature; the pre-flight checks the target is the ``journal_log`` of the
    account's own collection."""
    from core.wallet import collection_registry, erc6551
    data = str((tx or {}).get("data") or "")
    if data[:10].lower() != erc6551.EXECUTE_BATCH_SELECTOR:
        return None, None, "a journaled account call is executeBatch(…, JournalLog.log(entry))"
    try:
        legs = erc6551.decode_execute_batch(data)
    except Exception as exc:  # noqa: BLE001
        return None, None, f"the account batch does not decode ({exc})"
    want = 4 if intent.via_account_batch else 2
    if len(legs) != want:
        return None, None, (f"a journaled account call has exactly {want} legs (the action, then "
                            f"the journal entry), not {len(legs)}")
    j_to, j_value, j_data, j_op = legs[-1]
    if j_op != 0 or j_value != 0:
        return None, None, "the journal leg must be operation 0 with no value"
    try:
        chain_id = collection_registry.chain_id_of(intent.chain)
        logs = {p.journal_log: p.journal_prefix for p in collection_registry.profiles_on(chain_id)
                if p.journal_log}
    except Exception as exc:  # noqa: BLE001
        return None, None, f"the owner's collection registry cannot be trusted ({exc}); failing closed"
    if str(j_to).lower() not in logs:
        return None, None, f"the journal leg calls {j_to}, which is no pinned JournalLog on {intent.chain}"
    why = _journal_entry_refusal(intent, j_data, signer=signer, chain_id=chain_id,
                                 prefixes={pfx for addr, pfx in logs.items()
                                           if addr == str(j_to).lower()})
    if why:
        return None, None, why
    rest = legs[:-1]
    stripped = dict(tx)
    stripped["data"] = (erc6551.encode_execute_batch(rest) if intent.via_account_batch
                        else erc6551.encode_execute(*rest[0]))
    return stripped, str(j_to).lower(), None


def _pinned_collection_accounts(chain: str):
    """``(chain_id, {pinned collection address}, frozenset of every account address of every
    pinned collection's ids 0..max_supply)`` from the owner's collection registry. Raises when
    the registry cannot be trusted (the callers refuse)."""
    from core.wallet import collection_registry, erc6551
    chain_id = collection_registry.chain_id_of(chain)
    profiles = collection_registry.profiles_on(chain_id)
    pinned = {p.address for p in profiles}
    accounts = frozenset().union(*(
        erc6551.collection_account_addresses(chain_id, p.address, p.max_supply,
                                             implementation=v.implementation, salt=v.salt)
        for p in profiles for v in p.accounts))
    return chain_id, pinned, accounts


def _collection_account_destination_refusal(chain: str, tx: dict, nft_out) -> Optional[str]:
    """W2, the half a code read cannot see: an account that is NOT DEPLOYED YET has no code, so
    a pinned collection's token sent (or minted) to ``accountOf(k)`` for a token ``k`` not yet
    minted passes the contract's own check and later sits inside token k's account. Pure CREATE2
    math instead (``erc6551.collection_account_addresses``, every id up to each profile's
    ``max_supply``), against (a) every measured move of a pinned collection's token and (b)
    every ABI word of a call addressed to a pinned collection (a ``mint(to, …)`` /
    ``transferFrom(…, to, …)`` names its destination there). Static, no RPC. An untrusted
    collection registry refuses (fail closed)."""
    try:
        _chain_id, pinned, accounts = _pinned_collection_accounts(chain)
    except Exception as exc:  # noqa: BLE001
        # Owner decision 2026-10-01: an untrusted registry refuses only what it guards — a
        # transaction that moves an NFT (and the collection mint/reveal shapes, which refuse in
        # their own modules). A swap or a send with no NFT movement does not depend on it.
        if nft_out:
            return (f"the owner's collection registry cannot be trusted ({exc}) — the nesting "
                    f"rule cannot run for this NFT move; failing closed")
        return None
    if not pinned:
        return None
    why = ("{what} names {dest}, the ERC-6551 account address of a pinned collection's token "
           "(deployed or not) — a token inside such an account can own itself and is bricked "
           "forever")
    for (contract, std, dest, token_id, _amt) in nft_out or ():
        if str(contract).lower() in pinned and str(dest or "").lower() in accounts:
            return why.format(what=f"the move of {std} {contract} #{token_id}", dest=dest)
    to = str((tx or {}).get("to") or "").lower()
    data = str((tx or {}).get("data") or "").lower().removeprefix("0x")
    if to in pinned and len(data) >= 8:
        body = data[8:]
        for i in range(0, len(body) - 63, 64):
            word = body[i:i + 64]
            if word[:24] == "0" * 24 and ("0x" + word[24:]) in accounts:
                return why.format(what=f"a call to the pinned collection {to}", dest="0x" + word[24:])
    return None


def _future_account_refusal(intent: TxIntent, tx: dict, deltas, holder: str, rpc) -> Optional[str]:
    """C5: refuse any asset sent to ``accountOf(k)`` of a pinned collection when token ``k`` is
    NOT MINTED — native value, ERC-20, NFT, any shape. The candidates are every address the
    transaction names: the declared destination, the called contract, each measured transfer's
    counterparty, each measured NFT move, and every ABI word of the calldata (a router's
    ``recipient``, an ERC-20 ``transfer(to, …)``). A candidate inside a profile's offline account
    set (pure CREATE2) is checked with ``ownerOf(k)`` on the collection; a failed read refuses
    (fail closed). An untrusted registry stops only what it guards (owner decision 2026-10-01:
    the NFT-move rule above already refuses then)."""
    from core.wallet import abi, collection_registry, erc6551
    try:
        chain_id = collection_registry.chain_id_of(intent.chain)
        profiles = collection_registry.profiles_on(chain_id)
    except Exception:  # noqa: BLE001
        return None
    if not profiles:
        return None
    cands = {str(intent.to or ""), str((tx or {}).get("to") or "")}
    cands.update(str(dest) for (_t, dest, _a) in deltas.holder_transfers or ())
    cands.update(str(dest) for (_c, _s, dest, _i, _a) in deltas.holder_nft_out or ())
    data = str((tx or {}).get("data") or "").lower().removeprefix("0x")
    body = data[8:] if len(data) >= 8 else ""
    for i in range(0, len(body) - 63, 64):
        word = body[i:i + 64]
        if word[:24] == "0" * 24:
            cands.add("0x" + word[24:])
    cands = {c.lower() for c in cands if c} - {str(holder).lower(), _ZERO_ADDRESS.lower()}
    for addr in sorted(cands):
        for p in profiles:
            for v in p.accounts:
                k = erc6551.collection_account_token_id(chain_id, p.address, p.max_supply, addr,
                                                        implementation=v.implementation, salt=v.salt)
                if k is None:
                    continue
                try:
                    raw = rpc("eth_call", [{"to": p.address, "data": abi.encode_call(
                        "ownerOf", [{"type": "uint256"}], [k])}, "latest"])
                    owner = abi.decode([{"type": "address"}], raw)[0]
                    minted = str(owner).lower() != _ZERO_ADDRESS.lower()
                except Exception as exc:  # noqa: BLE001 — a revert IS "not minted"; so is a failure
                    minted, why = False, f" (ownerOf({k}) failed: {exc})"
                else:
                    why = ""
                if not minted:
                    return (f"the transaction names {addr}, the ERC-6551 account of {p.address} "
                            f"#{k}, a token not minted yet{why} — whoever mints #{k} later owns "
                            f"every asset sent there; failing closed")
    return None


def _collection_runtime_refusal(chain: str, collection: Optional[str], rpc) -> Optional[str]:
    """The pinned profile of *collection* on *chain*, re-checked against the live code."""
    from core.wallet import collection_registry
    try:
        profile = collection_registry.profile_for(collection_registry.chain_id_of(chain),
                                                  str(collection or ""))
    except Exception as exc:  # noqa: BLE001
        return f"the owner's collection registry cannot be trusted ({exc}); failing closed"
    if profile is None:
        return f"{collection} is not a pinned collection on {chain}"
    return collection_registry.runtime_refusal(rpc, profile)


def _same_addr(a, b) -> bool:
    """Case-insensitive address equality; an absent side never matches."""
    return bool(a) and bool(b) and str(a).strip().lower() == str(b).strip().lower()


def _pinned_nft_departure_refusal(chain: str, nft_out, rpc, *, holder: Optional[str] = None,
                                  accepted: Sequence[str] = ()) -> Optional[str]:
    """069 v4 §5 rule 5 (+ rule 3 for the collection's code). For every measured move of a
    PINNED collection's token out of the holder: the collection's live code must hash to the
    pinned ``runtime_sha256``, and the token's ERC-6551 account (every pinned account version)
    must have NO open approval — ERC-20 allowances, ERC-721 approvals, ApprovalForAll and
    Permit2 (``erc6551.open_approvals``, scanned from the profile's ``deploy_block`` to the
    head, every row live-confirmed; a row whose live read failed stays). The scan is complete
    by construction (the account cannot approve anything before its collection exists); any
    failed read is an incomplete scan and refuses. A token of an unpinned collection is not
    judged here.

    Any contract can emit an ``Approval`` naming the account, so a row whose events no
    transaction SENT by an owner of the NFT emitted (``erc6551.attribute``) may be fabricated
    and cannot always be revoked. Only such a row — never one an owner's transaction granted —
    may be accepted by the owner, by its key in ``accepted`` (the intent's
    ``accepted_unattributed_approvals``; the verb is always owner-approved and the card lists
    the keys)."""
    from core.wallet import collection_registry, erc6551
    try:
        chain_id = collection_registry.chain_id_of(chain)
        pinned = {p.address: p for p in collection_registry.profiles_on(chain_id)}
    except Exception as exc:  # noqa: BLE001
        return (f"the owner's collection registry cannot be trusted ({exc}) — the approval "
                f"rule cannot run for this NFT move; failing closed")
    head = None
    for (contract, std, _dest, token_id, _amt) in nft_out or ():
        profile = pinned.get(str(contract).lower())
        if profile is None:
            continue
        why = collection_registry.runtime_refusal(rpc, profile)
        if why:
            return why
        if "permit2" not in tuple(getattr(erc6551, "APPROVAL_KINDS", ())):
            return ("the approval scan does not cover Permit2 — an incomplete table is never "
                    "'empty'; failing closed")
        for version in profile.accounts:
            account = erc6551.account_address(chain_id, profile.address, int(token_id),
                                              salt=version.salt,
                                              implementation=version.implementation)
            try:
                if head is None:
                    head = int(rpc("eth_blockNumber", []), 16)
                rows = erc6551.open_approvals(rpc, account, profile.deploy_block, to_block=head)
            except Exception as exc:  # noqa: BLE001
                return (f"the approval scan of {account}, the account of {std} {contract} "
                        f"#{token_id}, did not complete ({exc}) — an incomplete table is never "
                        f"'empty', so the token may not leave; failing closed")
            if rows:
                try:
                    owners = erc6551.nft_owners(rpc, profile.address, int(token_id),
                                                profile.deploy_block, head)
                    if holder:
                        owners.add(str(holder).lower())
                    rows = erc6551.attribute(rpc, rows, owners)
                except Exception:  # noqa: BLE001 — origin unread: every row stays undecided
                    pass
                taken = {str(k).strip().lower() for k in accepted or ()}
                rows = [r for r in rows if r.attributed is True or r.key not in taken]
            if rows:
                def _origin(r):
                    if r.attributed is True:
                        return ""
                    why = ("no transaction an owner of this NFT sent emitted it"
                           if r.attributed is False else "its origin could not be read")
                    return f" [{why}; the owner may accept it as {r.key}]"
                shown = "; ".join(
                    f"{r.kind} {r.contract}"
                    + (f" #{r.token_id}" if r.token_id is not None else "")
                    + f" → {r.spender}" + ("" if r.verified else " (unconfirmed)") + _origin(r)
                    for r in rows[:5])
                more = f" (+{len(rows) - 5} more)" if len(rows) > 5 else ""
                return (f"{std} {contract} #{token_id} may not leave while its account "
                        f"{account} has {len(rows)} open approval(s): {shown}{more}. "
                        f"Approvals survive a transfer of the NFT, so the next owner's account "
                        f"could still be drained. Clear approvals first: agent_nft_revoke_all, "
                        f"then send it")
    return None


def _nft_nesting_refusal(chain: str, nft_out, rpc) -> Optional[str]:
    """W2. For each measured NFT move out of the holder, read the destination's code: when it is
    an ERC-6551 registry clone (any implementation) whose footer names the moved token's OWN
    collection — or a pinned collection when the moved token is from a pinned collection —
    refuse (a self-cycle or a nesting that can close a cycle). A clone bound to ANOTHER chain
    refuses too: there ``owner()`` is ``address(0)`` and the token is lost. Unreadable code
    refuses, and so does an untrusted collection registry."""
    from core.wallet import erc6551
    try:
        chain_id, pinned, _accounts = _pinned_collection_accounts(chain)
    except Exception as exc:  # noqa: BLE001
        return (f"the owner's collection registry cannot be trusted ({exc}) — the nesting rule "
                f"cannot run; failing closed")
    for (contract, std, dest, token_id, _amt) in nft_out:
        d = str(dest or "").lower()
        if not d or int(d, 16) == 0 or d == _DEAD_ADDRESS.lower():
            continue  # a burn: no account to nest into
        try:
            clone = erc6551.read_clone(rpc, dest)
        except Exception as exc:  # noqa: BLE001
            return (f"could not read the code at {dest}, the destination of {std} {contract} "
                    f"#{token_id} ({exc}) — a token-bound account there could brick the token; "
                    f"failing closed")
        if clone is None:
            continue
        _impl, _salt, f_chain, f_contract, f_id = clone
        if f_chain != chain_id:
            return (f"{dest} is a token-bound account of chain {f_chain}, not {chain} "
                    f"({chain_id}) — here its owner() is address(0), so {std} {contract} "
                    f"#{token_id} sent there is lost")
        c = str(contract).lower()
        if f_contract.lower() == c or (c in pinned and f_contract.lower() in pinned):
            return (f"{std} {contract} #{token_id} would move into {dest}, the token-bound "
                    f"account of {f_contract} #{f_id} — a token inside an account of its own "
                    f"collection can own itself and is bricked forever")
    return None


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
