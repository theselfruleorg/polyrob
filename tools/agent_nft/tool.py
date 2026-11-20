"""The ``agent_nft`` tool — act through the token-bound account of an agent NFT this instance
OWNS (050 §7.6; 069 v4: the simple model — no grants, bindings or operator keys).

Every verb is core (123 S4: there is no package seam). Chains are not a fixed list here:
``chain`` is any EVM chain of the chain table. What an account can and cannot do is the kind
table's one sentence per kind (``core.wallet.account_kinds.limits_text``).

⚠️ This module deliberately does NOT ``from __future__ import annotations``: the
Registry introspects each action's first-parameter annotation to route the
validated param model, and stringized annotations break that.

Every verb here does the SAME three things and nothing else: the flag, the turn origin (+ the
031 pause for a write), then runs its body: snapshot, inspect, journal and bind
(``tools/agent_nft/core_verbs.py``), revoke_all and withdraw_token (``withdraw.py``), the mint
(``mint.py``), the reveal, the sale's claim and settle (``sale.py``) and the sale acts. There is
NO adopt verb: holding the NFT is the whole relation (proposal 115). ``bind`` registers the
account's ERC-8004 identity; there is no grant verb. Every write builds a ``TxIntent`` and calls
``tx_guard`` — core's ONE authorizer — through :meth:`AgentNftTool.guard`.
"""
import logging
import types
from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field

from core.wallet.account_kinds import limits_text as _kind_limits_text

from tools.act_reason_param import ActReason
from tools.base_tool import BaseTool
from tools.wallet_holder import WalletHolderMixin

logger = logging.getLogger(__name__)

FLAG = "AGENT_NFT_ENABLED"
_NULL_CONFIG = types.SimpleNamespace()


def agent_nft_enabled() -> bool:
    from core.env import bool_env
    return bool_env(FLAG, False)


_DRY = Field(True, description=(
    "TRUE (default) builds the transaction, simulates it through the guard and reports what WOULD "
    "happen, broadcasting nothing. Set false to actually send."))


def _evm_chain(value: str) -> str:
    """N3: any EVM chain of the chain table (the collection must be pinned there — checked at
    run time against the pinned profiles, so the schema stays the same on every host)."""
    from core.wallet import chains
    row = chains.get(value)
    if row is None or row.family != "evm":
        raise ValueError(f"{value!r} is not an EVM chain of the chain table — name the chain "
                         f"where the NFT's collection is pinned")
    return value


#: The chain of an agent-NFT verb: a chain-table name, folded like every money verb's.
AgentNftChain = Annotated[str, BeforeValidator(
    lambda v: v.strip().lower() if isinstance(v, str) else v), AfterValidator(_evm_chain)]

_CHAIN = Field(..., description=(
    "The EVM chain of the NFT's collection, by its chain-table name. No pin is needed: an "
    "unpinned collection is read from the chain and its money acts wait for the owner's card."))
#: The kinds' limits, one sentence per account kind (the same text the views and the skill use).
_KINDS = _kind_limits_text()
_NFT = Field(None, description=(
    "Which NFT: '<collection>#<id>', or '<id>' when one collection is pinned on the chain. Omit "
    "when this treasury holds exactly one tracked NFT there."))
_ACCOUNT = Field(None, pattern=r"^0x[0-9a-fA-F]{40}$", description=(
    "Or name the NFT by its token-bound account address."))


class SnapshotParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # C10: two NFTs on one chain must be nameable.
    chain: AgentNftChain = _CHAIN
    nft: Optional[str] = _NFT
    account: Optional[str] = _ACCOUNT


class InspectParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(..., description=(
        "Any agent NFT, as '<chain>:<collection>/<token_id>' (e.g. 'base:0xabc…/42') or "
        "'<chain>:<token_id>' on the chain's one pinned collection. Read-only: owner, holdings, the "
        "account kind's lock or sale readiness, state, identity count and — FIRST — every open "
        "approval on the account."))


class JournalParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(..., description=(
        "'thesis' or 'note'. entry/exit/tend are written by the money verbs themselves; "
        "handover marks a change of owner."))
    text: str = Field(..., min_length=1, max_length=4000, description=(
        "The opinion to record. Signed by the NFT's owner key (this treasury), chained."))
    anchor: bool = Field(False, description=(
        "Also anchor the journal head on-chain now (setMetadata through the account; fee only)."))
    max_spend_usd: float = Field(1.0, gt=0, description="Most USD of FEE authorized for an anchor.")
    chain: AgentNftChain = _CHAIN
    nft: Optional[str] = _NFT
    account: Optional[str] = _ACCOUNT
    dry_run: bool = _DRY


class BindParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_uri: str = Field("", max_length=16_384, description=(
        "Leave empty: bind writes the account's own registration document (agentWallet = the "
        "account); alternate URIs are refused."))
    max_spend_usd: float = Field(2.0, gt=0, description="Most USD of FEE authorized.")
    chain: AgentNftChain = _CHAIN
    nft: Optional[str] = _NFT
    account: Optional[str] = _ACCOUNT
    dry_run: bool = _DRY


class RevokeAllParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chain: AgentNftChain = _CHAIN
    nft: Optional[str] = _NFT
    account: Optional[str] = _ACCOUNT
    max_spend_usd: float = Field(2.0, gt=0, description="Most USD of FEE authorized for the whole sweep.")
    include_unattributed: bool = Field(False, description=(
        "Also call into contracts whose approval rows no transaction an owner of this NFT sent "
        "(possibly fabricated: fees spent there may be wasted). Default: list them, do not call."))
    dry_run: bool = _DRY


#: One whitelist proof word: 0x + 64 hex (a bytes32).
_PROOF_WORD = Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{64}$")]


class MintParams(ActReason):   # why: audit 2026-11-21 L-3
    """A mint through the collection's ``SALE()``, read from the chain (124 section 7): the
    sale's phase picks ``mintWL(to, qty, minOutPerToken, allowance, proof)`` (the whitelist) or
    ``mint(to, qty, minOutPerToken)`` (the public phase, and after the settlement)."""
    model_config = ConfigDict(extra="forbid")
    to: Optional[str] = Field(None, pattern=r"^0x[0-9a-fA-F]{40}$", description=(
        "Must be this instance's treasury, or omitted (the default, the same thing). Any other "
        "address is refused: the guard must see every minted token arrive in the paying wallet."))
    qty: int = Field(1, ge=1, le=100, description=(
        "How many to mint in one call; the sale's own per-call cap (read from it) applies. "
        "The quote names the price."))
    allowance: Optional[int] = Field(None, ge=1, description=(
        "Whitelist phase only: this treasury's allowance in the published whitelist (its Merkle "
        "leaf is (treasury, allowance)). Omit outside the whitelist phase."))
    proof: Optional[list[_PROOF_WORD]] = Field(None, max_length=32, description=(
        "Whitelist phase only: the Merkle proof of (treasury, allowance), each word 0x + 64 hex, "
        "at most 32. Omit outside the whitelist phase."))
    min_out_per_token: Optional[int] = Field(None, ge=0, description=(
        "The sale's minOutPerToken argument: the least each new account must receive from the "
        "sale's buy, else the mint reverts. 0 (the default) = never revert on price."))
    chain: AgentNftChain = _CHAIN
    collection: Optional[str] = Field(None, pattern=r"^0x[0-9a-fA-F]{40}$", description=(
        "The collection to mint (any ERC-721 whose SALE() names it back). May be left out only "
        "when exactly one collection with a sale is pinned on the chain."))
    max_spend_usd: float = Field(..., gt=0, description=(
        "Most USD authorized: the value sent (qty × the sale's price now) plus gas."))
    dry_run: bool = _DRY


class TakeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: Optional[str] = Field(None, pattern=r"^0x[0-9a-fA-F]{40}$", description=(
        "Where the NFT goes (its account goes with it): a plain wallet. Refused: an account of "
        "its own collection, deployed or not yet minted (the token could own itself), and this "
        "treasury. Required unless `nested` is given."))
    nested: Optional[str] = Field(None, pattern=r"^0x[0-9a-fA-F]{40}#[0-9]+$", description=(
        "124 N2, the sweep: <collection>#<id> of an NFT that the ACCOUNT of `nft` holds (a nested "
        "NFT). It moves from that account to this treasury (the only destination; `to` must be "
        "left out). Use it before acting with the nested NFT — there is no act through a second "
        "level."))
    chain: AgentNftChain = _CHAIN
    nft: Optional[str] = _NFT
    account: Optional[str] = _ACCOUNT
    max_spend_usd: float = Field(2.0, gt=0, description="Most USD of FEE authorized.")
    accept_unattributed_approvals: list[str] = Field(default_factory=list, max_length=20, description=(
        "OWNER ONLY: keys of open-approval rows that no transaction an owner of this NFT sent "
        "emitted (the refusal names them). Each may be fabricated by any contract and so not "
        "revocable; accepting one lets the NFT leave with it. A row an owner granted always blocks."))
    dry_run: bool = _DRY


class RevealParams(ActReason):   # why: audit 2026-11-21 L-3
    """``reveal(uint256[] ids)`` on a collection (any ERC-721; every reveal is the owner's card
    unless a mandate covers it)."""
    model_config = ConfigDict(extra="forbid")
    chain: AgentNftChain = _CHAIN
    collection: Optional[str] = Field(None, pattern=r"^0x[0-9a-fA-F]{40}$", description=(
        "The collection to reveal; may be left out when exactly one collection is pinned on "
        "the chain."))
    max_ids: Optional[int] = Field(None, ge=1, le=64, description=(
        "Most due ids revealed in one transaction, in id order: an upper bound; default = sized "
        "to the fee cap."))
    max_spend_usd: Optional[float] = Field(None, gt=0, description=(
        "Most USD of gas FEE authorized; never above AGENT_NFT_REVEAL_MAX_GAS_USD (the default)."))
    dry_run: bool = _DRY


class SaleClaimParams(BaseModel):
    """``claim(treasury)`` on the pinned sale: what ``owed(treasury)`` holds (a mint's odd wei,
    or a refund push that failed)."""
    model_config = ConfigDict(extra="forbid")
    chain: AgentNftChain = _CHAIN
    collection: Optional[str] = Field(None, pattern=r"^0x[0-9a-fA-F]{40}$", description=(
        "Only when more than one collection with a sale is pinned on the chain."))
    max_spend_usd: float = Field(1.0, gt=0, description="Most USD of gas FEE authorized.")
    dry_run: bool = _DRY


class SaleSettleParams(BaseModel):
    """``settle(records)`` on the pinned sale, for this treasury's own open auction records: the
    sale pays each record's excess above the clearing price back to the treasury."""
    model_config = ConfigDict(extra="forbid")
    chain: AgentNftChain = _CHAIN
    collection: Optional[str] = Field(None, pattern=r"^0x[0-9a-fA-F]{40}$", description=(
        "Only when more than one collection with a sale is pinned on the chain."))
    records: Optional[list[Annotated[int, Field(ge=0)]]] = Field(None, max_length=20, description=(
        "Record ids to settle (each must be this treasury's). Omit: the sale's Bid logs find "
        "every open record of this treasury (at most 20 per call)."))
    max_spend_usd: float = Field(2.0, gt=0, description="Most USD of gas FEE authorized.")
    dry_run: bool = _DRY


class SaleReadyParams(BaseModel):
    """``readyToSell(id)`` by the NFT's own account (115 W7), or ``cancelReady(id)``."""
    model_config = ConfigDict(extra="forbid")
    chain: AgentNftChain = _CHAIN
    nft: Optional[str] = _NFT
    account: Optional[str] = _ACCOUNT
    max_spend_usd: float = Field(1.0, gt=0, description="Most USD of gas FEE authorized.")
    dry_run: bool = _DRY


class SaleCancelListingParams(BaseModel):
    """``cancel(salt)`` or ``incrementNonce()`` on the collection's pinned market (115 W7)."""
    model_config = ConfigDict(extra="forbid")
    chain: AgentNftChain = _CHAIN
    collection: Optional[str] = Field(None, pattern=r"^0x[0-9a-fA-F]{40}$", description=(
        "Only when more than one collection with a pinned market is on the chain."))
    salt: Optional[int] = Field(None, ge=0, description=(
        "The salt of the ONE order to cancel. Omit to cancel EVERY order this treasury signed on "
        "the market (incrementNonce)."))
    nft: Optional[str] = Field(None, description=(
        "Also revoke the market's approval of this NFT ('<collection>#<id>' or '<id>'). Without "
        "it, the approvals of the ids whose open orders the cancel ends are revoked."))
    max_spend_usd: float = Field(1.0, gt=0, description="Most USD of gas FEE authorized.")
    dry_run: bool = _DRY


class SaleListParams(BaseModel):
    """A market ``Listing`` of an NFT this treasury owns, signed as class O (111 W9a)."""
    model_config = ConfigDict(extra="forbid")
    chain: AgentNftChain = _CHAIN
    nft: Optional[str] = _NFT
    account: Optional[str] = _ACCOUNT
    price: str = Field(..., pattern=r"^[0-9]+(\.[0-9]+)?$", max_length=40, description=(
        "The price in whole units of the currency (e.g. '0.05' ETH)."))
    currency: str = Field("native", max_length=42, description=(
        "'native' (ETH, the default) or the address of a token on core's reviewed list."))
    expiry_days: int = Field(7, ge=1, le=30, description="Days the listing stays valid (1-30).")
    tolerance_bps: int = Field(0, ge=0, le=50, multiple_of=50, description=(
        "The seller's promise: 0 (default) every promised holding stays whole until the fill; 50 "
        "lets the agent spend up to 0.5 % of each while listed."))
    max_spend_usd: float = Field(1.0, gt=0, description=(
        "Most USD of gas FEE authorized (the market's approval, when it is needed)."))
    dry_run: bool = _DRY


class SaleAcceptParams(BaseModel):
    """``accept(offer, …)`` of a buyer's signed Offer on the pinned market (111 W9a)."""
    model_config = ConfigDict(extra="forbid")
    chain: AgentNftChain = _CHAIN
    offer: str = Field(..., min_length=8, max_length=64_000, description=(
        "The offer the buyer shared: its link (the part after '#x=' is read), the bare code, or "
        "its JSON ({v: 1, k: 'o', offer, sig, conditions, market, chainId})."))
    nft: Optional[str] = Field(None, description=(
        "The NFT to sell ('<collection>#<id>' or '<id>'); required only for an offer on any id."))
    max_spend_usd: float = Field(1.0, gt=0, description="Most USD of gas FEE authorized.")
    dry_run: bool = _DRY


class BuyParams(ActReason):   # why: audit 2026-11-21 L-3
    """``buy(listing, …)`` on a pinned collection's market (115 W9): the treasury pays the
    listing's ETH price; the buyer's conditions come from a fresh read of the NFT's account."""
    model_config = ConfigDict(extra="forbid")
    chain: AgentNftChain = _CHAIN
    listing: str = Field(..., min_length=8, max_length=64_000, description=(
        "The market listing the seller shared: its link (the part after '#x=' is read), the "
        "bare code, or its JSON ({v: 1, k: 'm', listing, sig, promise, market, chainId})."))
    tolerance_bps: int = Field(0, ge=0, le=50, multiple_of=50, description=(
        "0 (default): every holding must be all there at the fill. 50: the seller may still "
        "take up to 0.5 % of each checked holding first (an active agent's account moves)."))
    conditions_hash: Optional[str] = Field(None, pattern=r"^0x[0-9a-fA-F]{64}$", description=(
        "The conditions_hash the quote (dry_run=true) printed. Leave it out for the quote; a real "
        "buy (dry_run=false) without it refuses. The buy then checks exactly what that quote "
        "showed; if the account changed since, it refuses and nothing is paid."))
    max_spend_usd: float = Field(..., gt=0, description=(
        "Most USD authorized: the listing's price plus gas."))
    dry_run: bool = _DRY


class MarketClaimParams(BaseModel):
    """``claim(treasury)`` on a pinned collection's market: what ``owed(treasury)`` holds."""
    model_config = ConfigDict(extra="forbid")
    chain: AgentNftChain = _CHAIN
    collection: Optional[str] = Field(None, pattern=r"^0x[0-9a-fA-F]{40}$", description=(
        "Only when more than one collection with a pinned market is on the chain."))
    max_spend_usd: float = Field(1.0, gt=0, description="Most USD of gas FEE authorized.")
    dry_run: bool = _DRY


class AgentNftTool(WalletHolderMixin, BaseTool):
    """An agent NFT this instance owns: act FROM its ERC-6551 token-bound account as the NFT's owner."""

    def __init__(self, name: str = "agent_nft", config=None, container=None, *,
                 wallet=None, guard_fn=None, rpc_fn=None, price_fn=None,
                 rail_factory=None):
        super().__init__(name=name, config=config if config is not None else _NULL_CONFIG,
                         container=container)
        self._wallet = wallet
        self._guard_fn = guard_fn
        self._rpc_fn = rpc_fn
        self._price_fn = price_fn
        self._rail_factory = rail_factory

    async def _initialize(self):
        return True

    # -- plumbing ---------------------------------------------------------------

    def rpc_for(self, chain: str):
        """``callable(method, params)`` on the chain's pinned RPC."""
        if self._rpc_fn is not None:
            return self._rpc_fn
        from core.wallet.onchain import _rpc, rpc_url_for_chain
        url = rpc_url_for_chain(chain)
        return lambda method, params: _rpc(url, method, params, timeout=15.0)

    def guard(self, intent, tx, **kw):
        """THE authorizer (tx_guard.authorize)."""
        if self._guard_fn is not None:
            return self._guard_fn(intent, tx, **kw)
        from core.wallet import tx_guard
        return tx_guard.authorize(intent, tx, **kw)

    def _gate(self, execution_context, verb: str, *, write: bool, dry_run: bool = True,
              entry: bool = True):
        if not agent_nft_enabled():
            from core.remedy import owner_flag_remedy
            return f"agent NFTs are off — {owner_flag_remedy(FLAG)}. Nothing was broadcast."
        from core.money.authorize import SpendIntent, authorize_spend
        from tools.controller.turn_origin import (
            _is_forged_or_autonomous_turn as _owner_turn_probe)
        verdict = authorize_spend(
            SpendIntent(tool="agent_nft", what=verb, entry=entry, pause=write,
                        dry_run=dry_run), execution_context,
            forged_fn=_owner_turn_probe)
        if verdict.refused:
            suffix = " RESULT: NOT SENT." if verdict.step == "pause" else ""
            return verdict.reason + suffix
        return None

    # -- core seams (J1, C3) ----------------------------------------------------

    def journal_history(self, params):
        """``(held, entries)``: the NFT named by *params* (chain / nft / account) and its verified
        journal chain (on chain when a ``journal_log`` is pinned). Raises when unreadable."""
        from tools.agent_nft import journal_write
        from tools.agent_nft.withdraw import _held
        held, _signer = _held(self, params, None)
        return held, journal_write.history(self, held)

    async def write_journal_entry(self, params, *, kind: str, text: str, execution_context=None):
        """One entry with no action (thesis / note / handover): on chain through the account
        when a ``journal_log`` is pinned, else the local journal. Honours ``params.dry_run`` and
        ``params.max_spend_usd``."""
        from tools.agent_nft import journal_write
        from tools.agent_nft.withdraw import _held
        held, signer = _held(self, params, execution_context)
        return await journal_write.write_entry(
            self, held=held, signer=signer, kind=kind, text=text,
            dry_run=getattr(params, "dry_run", True),
            max_spend_usd=getattr(params, "max_spend_usd", 1.0),
            execution_context=execution_context)

    # -- reads ----------------------------------------------------------------

    @BaseTool.action(
        "The agent NFT account this instance owns: every OPEN APPROVAL found and whether that table is "
        "complete, the purchase checks, owner (must be this treasury), the account kind's lock or sale "
        "readiness, state, identity, reveal state, native balance, journal entry count and head. "
        "Read-only. " + _KINDS,
        param_model=SnapshotParams)
    async def agent_nft_snapshot(self, params: SnapshotParams, execution_context=None):
        return await self._core_body("snapshot", params, execution_context, write=False)

    @BaseTool.action(
        "Inspect ANY agent NFT before buying or taking it: open approvals FIRST with their coverage "
        "(complete or incomplete, never 'empty' when incomplete), then named checks (approval table, the "
        "account kind's lock or sale readiness, revealed), owner, state, identity count, native balance. "
        "Read-only.",
        param_model=InspectParams)
    async def agent_nft_inspect(self, params: InspectParams, execution_context=None):
        return await self._core_body("inspect", params, execution_context, write=False)

    # -- writes (every one through tx_guard with via_account) ---------------------

    @BaseTool.action(
        "Record a signed, chained journal entry (thesis or note) for the account of an NFT this "
        "instance owns; optionally anchor the head on-chain through the account (fee only).",
        param_model=JournalParams)
    async def agent_nft_journal(self, params: JournalParams, execution_context=None):
        return await self._core_body("journal", params, execution_context, write=True)

    @BaseTool.action(
        "Publish the agent's card on the owned NFT account's ERC-8004 identity THROUGH the account "
        "(agentWallet = the account): setAgentURI on the identity the account owns (an ERC-6551P "
        "token has one from its mint), or register() when it has none. Never a second identity. "
        "Fee only.",
        param_model=BindParams)
    async def agent_nft_bind_identity(self, params: BindParams, execution_context=None):
        return await self._core_body("bind", params, execution_context, write=True)

    async def _core_body(self, verb: str, params, execution_context, *, write: bool):
        """C20: snapshot / inspect / journal / bind are core-owned (``tools/agent_nft/core_verbs.py``)
        and need no package."""
        err = self._gate(execution_context, verb, write=write,
                         dry_run=getattr(params, "dry_run", True))
        if err:
            return self._ar(error=err)
        from tools.agent_nft import core_verbs
        try:
            return await getattr(core_verbs, verb)(self, params, execution_context)
        except Exception as exc:  # noqa: BLE001 — a verb failure is an error result, never a crash
            logger.warning("agent_nft %s failed", verb, exc_info=True)
            return self._ar(error=f"agent_nft {verb} failed: {exc}. Nothing was broadcast unless a "
                                  f"tx hash is shown.")

    @BaseTool.action(
        "Revoke the open approvals the approval scan finds on the owned NFT account (ERC-20 "
        "allowances, ApprovalForAll grants, single-token NFT approvals, Permit2 allowances, ERC-6909 "
        "allowances and operators), through "
        "the account, one guarded call each — do this before the NFT leaves. NOT revoked: anything "
        "the scan does not find (an approval table reported incomplete stays incomplete). Fee only.",
        param_model=RevokeAllParams)
    async def agent_nft_revoke_all(self, params: RevokeAllParams, execution_context=None):
        # Core-owned body (069 v4 §5 rule 5: the guard names this verb as the remedy, so it
        # must not depend on the optional package). Risk-reducing: entry=False.
        return await self._core("revoke_all", params, execution_context, entry=False)

    @BaseTool.action(
        "Mint tokens of a collection through its SALE() (read from the chain) to this instance's "
        "treasury (the treasury then owns them and their accounts) at the sale's price now "
        "(whitelist: the base price, with allowance + proof; public: the price now plus one step, "
        "the excess comes back at settlement). Any ERC-721 whose sale names it back; a pinned "
        "collection keeps its pin's price cap. ALWAYS owner-approved.",
        param_model=MintParams)
    async def agent_nft_collection_mint(self, params: MintParams, execution_context=None):
        # 123 S3 / 124 section 7: a core body; no package, no pin needed.
        err = self._gate(execution_context, "mint", write=True, dry_run=params.dry_run,
                         entry=True)
        if err:
            return self._ar(error=err)
        from tools.agent_nft.mint import mint
        try:
            return await mint(self, params, execution_context)
        except Exception as exc:  # noqa: BLE001 — a verb failure is an error result, never a crash
            logger.warning("agent_nft mint failed", exc_info=True)
            return self._ar(error=f"agent_nft mint failed: {exc}. Nothing was broadcast unless a "
                                  f"tx hash is shown.")

    @BaseTool.action(
        "Send an agent NFT this treasury owns to an address `to` (its account goes with it). "
        "Refused while its account has ANY open approval — run agent_nft_revoke_all first. "
        "With `nested=<collection>#<id>`: sweep an NFT that the account holds back to this "
        "treasury. ALWAYS owner-approved.",
        param_model=TakeParams)
    async def agent_nft_withdraw_token(self, params: TakeParams, execution_context=None):
        # Core-owned body (069 v4 A4): the owner's `/nft send <id> <to>` runs it.
        return await self._core("withdraw", params, execution_context, entry=False)

    async def _core(self, verb: str, params, execution_context, *, entry: bool):
        err = self._gate(execution_context, verb, write=True, dry_run=params.dry_run,
                         entry=entry)
        if err:
            return self._ar(error=err)
        from tools.agent_nft import withdraw as _bodies
        try:
            return await getattr(_bodies, verb)(self, params, execution_context)
        except Exception as exc:  # noqa: BLE001 — a verb failure is an error result, never a crash
            logger.warning("agent_nft %s failed", verb, exc_info=True)
            return self._ar(error=f"agent_nft {verb} failed: {exc}. Nothing was broadcast unless a "
                                  f"tx hash is shown.")

    @BaseTool.action(
        "Reveal every DUE id of a collection: reveal(ids) from the treasury (the owner's card "
        "unless a mandate covers it), "
        "in id order, up to max_ids per call. Spends gas only (no value, no asset; at most "
        "AGENT_NFT_REVEAL_MAX_GAS_USD of fee); the guard requires one Revealed/Recommitted per id "
        "and no other movement. Nothing due = no transaction.",
        param_model=RevealParams)
    async def agent_nft_collection_reveal(self, params: RevealParams, execution_context=None):
        # Core-owned body (tools/agent_nft/reveal.py): no agent-NFT package needed, and a
        # reveal opens no position, so the scoped `/pause trading` does not hold it (the
        # full owner pause does — here and again in tx_guard).
        err = self._gate(execution_context, "reveal", write=True, dry_run=params.dry_run,
                         entry=False)
        if err:
            return self._ar(error=err)
        from tools.agent_nft import reveal
        try:
            return await reveal.run(self, params, execution_context)
        except Exception as exc:  # noqa: BLE001 — a verb failure is an error result, never a crash
            logger.warning("agent_nft collection reveal failed", exc_info=True)
            return self._ar(error=f"agent_nft collection reveal failed: {exc}. Nothing was broadcast unless a tx "
                                  f"hash is shown.")

    @BaseTool.action(
        "Take back what the pinned sale OWES this treasury: claim(treasury) pays owed() (a mint's "
        "odd wei, or a refund that could not be pushed). Sends nothing; the guard requires the "
        "measured native receipt and one Claimed event. Gas only. Nothing owed = no transaction.",
        param_model=SaleClaimParams)
    async def agent_nft_sale_claim(self, params: SaleClaimParams, execution_context=None):
        return await self._sale("claim", params, execution_context)

    @BaseTool.action(
        "After the sale is settled: settle(records) for this treasury's own open auction records, "
        "so the sale pays back each record's excess above the clearing price (and the tokens' "
        "accounts get their top-ups). Sends nothing; the guard requires the measured refund and one "
        "Settled per record. Gas only. No open record or no refund = no transaction.",
        param_model=SaleSettleParams)
    async def agent_nft_sale_settle(self, params: SaleSettleParams, execution_context=None):
        return await self._sale("settle", params, execution_context)

    @BaseTool.action(
        "Make an agent NFT this treasury owns READY FOR SALE: its own account calls the "
        "collection's readyToSell (with the journal entry, one transaction). From the next block a "
        "plain marketplace fill may move the NFT and everything its account holds, with no buyer "
        "check. Any act through the account ends readiness, so while ready the guard refuses "
        "every automatic act through it. Moves nothing. ALWAYS owner-approved. A listing on the "
        "pinned market (agent_nft_sale_list) needs no readiness.",
        param_model=SaleReadyParams)
    async def agent_nft_sale_ready(self, params: SaleReadyParams, execution_context=None):
        return await self._sale_act("ready", params, execution_context)

    @BaseTool.action(
        "End the sale readiness of an agent NFT this treasury owns: cancelReady from the treasury "
        "(the account is not used). Moves nothing; a plain marketplace fill then reverts. Not "
        "ready = no transaction.",
        param_model=SaleReadyParams)
    async def agent_nft_sale_unready(self, params: SaleReadyParams, execution_context=None):
        return await self._sale_act("unready", params, execution_context)

    @BaseTool.action(
        "Withdraw this treasury's orders on a collection's PINNED market: cancel(salt) ends one "
        "order; without a salt, incrementNonce ends every listing and offer it signed there; then "
        "the market's single-token approval of each id whose order ended is revoked. Moves "
        "nothing; the guard requires the market's event and the nonce or salt read after.",
        param_model=SaleCancelListingParams)
    async def agent_nft_sale_cancel_listing(self, params: SaleCancelListingParams,
                                            execution_context=None):
        return await self._sale_act("cancel_listing", params, execution_context)

    @BaseTool.action(
        "LIST an agent NFT this treasury owns on its collection's PINNED market: the treasury "
        "signs ONE market Listing (price, currency native or a reviewed token, 1-30 days); core "
        "builds every field and the seller's promise from a fresh read of the account. The market "
        "first gets the single-token approval of that NFT (one guarded transaction, never "
        "setApprovalForAll). Returns the share link the desk site and agent_nft_buy read. A "
        "listing binds the NFT's transfer count, not readiness: I keep working while listed. "
        "ALWAYS owner-approved. No Seaport or OpenSea listing, no offer of my own.",
        param_model=SaleListParams)
    async def agent_nft_sale_list(self, params: SaleListParams, execution_context=None):
        return await self._sale_order("list_", params, execution_context)

    @BaseTool.action(
        "ACCEPT a buyer's signed Offer (the link the buyer shared) for an agent NFT this treasury "
        "owns, on its collection's PINNED market: the market's single-token approval if needed, "
        "then accept(offer, …); the guard requires exactly that NFT out to the buyer, the ERC-20 "
        "price less royalty in, one Sold, the approval consumed. The NFT and everything its "
        "account holds leave. ALWAYS owner-approved.",
        param_model=SaleAcceptParams)
    async def agent_nft_sale_accept(self, params: SaleAcceptParams, execution_context=None):
        return await self._sale_order("accept", params, execution_context)

    async def _sale_order(self, verb: str, params, execution_context):
        # Core-owned (tools/agent_nft/sale_list.py, 111 W9a): money TOWARD the treasury.
        err = self._gate(execution_context, f"sale_{verb.rstrip('_')}", write=True,
                         dry_run=params.dry_run, entry=False)
        if err:
            return self._ar(error=err)
        from tools.agent_nft import sale_list
        try:
            return await getattr(sale_list, verb)(self, params, execution_context)
        except Exception as exc:  # noqa: BLE001 — a verb failure is an error result, never a crash
            logger.warning("agent_nft sale %s failed", verb, exc_info=True)
            return self._ar(error=f"agent_nft sale {verb.rstrip('_')} failed: {exc}. Nothing was "
                                  f"signed or broadcast unless a tx hash or a link is shown.")

    async def _sale_act(self, verb: str, params, execution_context):
        # Core-owned (tools/agent_nft/sale_ready.py): moves nothing and opens no position.
        err = self._gate(execution_context, f"sale_{verb}", write=True, dry_run=params.dry_run,
                         entry=False)
        if err:
            return self._ar(error=err)
        from tools.agent_nft import sale_ready
        try:
            return await getattr(sale_ready, verb)(self, params, execution_context)
        except Exception as exc:  # noqa: BLE001 — a verb failure is an error result, never a crash
            logger.warning("agent_nft sale %s failed", verb, exc_info=True)
            return self._ar(error=f"agent_nft sale {verb} failed: {exc}. Nothing was broadcast "
                                  f"unless a tx hash is shown.")

    async def _sale(self, verb: str, params, execution_context):
        # Core-owned (tools/agent_nft/sale.py): money TOWARD the treasury, so not an entry.
        err = self._gate(execution_context, f"sale_{verb}", write=True, dry_run=params.dry_run,
                         entry=False)
        if err:
            return self._ar(error=err)
        from tools.agent_nft import sale
        try:
            return await getattr(sale, verb)(self, params, execution_context)
        except Exception as exc:  # noqa: BLE001 — a verb failure is an error result, never a crash
            logger.warning("agent_nft sale %s failed", verb, exc_info=True)
            return self._ar(error=f"agent_nft sale {verb} failed: {exc}. Nothing was broadcast "
                                  f"unless a tx hash is shown.")

    @BaseTool.action(
        "Buy an agent NFT listed on its collection's PINNED market (a market listing link the "
        "seller shared; ETH price only): the treasury pays the price and receives the NFT with "
        "its account. The buyer's conditions are built from a fresh read of that account (every "
        "holding), and the market checks them after the transfer in the same transaction: if "
        "the seller empties the account first the fill reverts and nothing is paid. The quote "
        "shows what the account holds and what is not checked. ALWAYS owner-approved.",
        param_model=BuyParams)
    async def agent_nft_buy(self, params: BuyParams, execution_context=None):
        return await self._buyer("buy", params, execution_context, entry=True)

    @BaseTool.action(
        "Take back what a collection's PINNED market owes this treasury: claim(treasury) pays "
        "owed() (an ETH payment it refused at a fill). Sends nothing; the guard requires the "
        "measured receipt and owed() at 0 after. Gas only. Nothing owed = no transaction.",
        param_model=MarketClaimParams)
    async def agent_nft_market_claim(self, params: MarketClaimParams, execution_context=None):
        return await self._buyer("claim", params, execution_context, entry=False)

    async def _buyer(self, verb: str, params, execution_context, *, entry: bool):
        # Core-owned (tools/agent_nft/sale_buy.py, 115 W9): a buy spends (an entry); a claim
        # moves money TOWARD the treasury.
        err = self._gate(execution_context, verb, write=True, dry_run=params.dry_run,
                         entry=entry)
        if err:
            return self._ar(error=err)
        from tools.agent_nft import sale_buy
        try:
            return await getattr(sale_buy, verb)(self, params, execution_context)
        except Exception as exc:  # noqa: BLE001 — a verb failure is an error result, never a crash
            logger.warning("agent_nft %s failed", verb, exc_info=True)
            return self._ar(error=f"agent_nft {verb} failed: {exc}. Nothing was broadcast "
                                  f"unless a tx hash is shown.")


def _register_card_context() -> None:
    """The withdraw approval card shows what the NFT's account holds before consent."""
    from tools.controller.grant_card import register_card_context
    from tools.agent_nft.withdraw import withdraw_card_context
    register_card_context("agent_nft_withdraw_token", withdraw_card_context)
    # 115 W7: a ready NFT can leave by a plain fill — the card shows what would go with it.
    register_card_context("agent_nft_sale_ready", withdraw_card_context)
    # 115 W9: a buy's card shows what the NFT's account holds and what the market will check.
    from tools.agent_nft.sale_buy import buy_card_context
    register_card_context("agent_nft_buy", buy_card_context)
    # 111 W9a (D9): one card per listing and per accept — the order and what leaves.
    from tools.agent_nft.sale_list import accept_card_context, list_card_context
    register_card_context("agent_nft_sale_list", list_card_context)
    register_card_context("agent_nft_sale_accept", accept_card_context)


_register_card_context()
