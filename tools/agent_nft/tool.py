"""The ``agent_nft`` tool — act through the token-bound account of an agent NFT this instance
OWNS (050 §7.6; 069 v4: the simple model — no grants, bindings or operator keys).

The package verbs are forwarded to the optional agent-NFT package (``polyrob_drop``;
``core.tool_capabilities.AGENT_NFT_PACKAGE_MODULES`` lists the import names it may have).

⚠️ This module deliberately does NOT ``from __future__ import annotations``: the
Registry introspects each action's first-parameter annotation to route the
validated param model, and stringized annotations break that.

Every verb here does the SAME three things and nothing else: the flag, the turn
origin (+ the 031 pause for a write), then hands the validated params to
``<package>.verbs.<verb>(tool, params, execution_context)`` (package verb names:
snapshot, inspect, journal, bind, mint). ``revoke_all`` and ``withdraw_token`` are core-owned
(``tools/agent_nft/withdraw.py``, 069 v4 A4), like the collection reveal. ``bind`` registers the
account's ERC-8004 identity; there is no grant verb. The package
builds the intent (``TxIntent.via_account``) and calls ``tx_guard`` — core's ONE
authorizer — through :meth:`AgentNftTool.guard`. A missing package is a refusal with
a remedy, never an import error at registration.
"""
import logging
import types
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

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


class SnapshotParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InspectParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(..., description=(
        "Any agent NFT, as '<chain>:<collection>/<token_id>' (e.g. 'robinhood:0xabc…/42') or just "
        "the token id on the pinned collection. Read-only: owner, holdings, lock, state, identity "
        "count and — FIRST — every open approval on the account."))


class JournalParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(..., description=(
        "'thesis' or 'note'. entry/exit/tend are reserved for the rail (no rail writes them yet); "
        "handover marks a change of owner."))
    text: str = Field(..., min_length=1, max_length=4000, description=(
        "The opinion to record. Signed by the NFT's owner key (this treasury), chained."))
    anchor: bool = Field(False, description=(
        "Also anchor the journal head on-chain now (setMetadata through the account; fee only)."))
    max_spend_usd: float = Field(1.0, gt=0, description="Most USD of FEE authorized for an anchor.")
    dry_run: bool = _DRY


class BindParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_uri: str = Field("", description="Optional registration-file URI for the account's identity.")
    max_spend_usd: float = Field(2.0, gt=0, description="Most USD of FEE authorized.")
    dry_run: bool = _DRY


_CHAIN = Field("robinhood", description="'robinhood' (4663) or 'robinhood-testnet' (46630).")
_NFT = Field(None, description=(
    "Which NFT: '<collection>#<id>', or '<id>' when one collection is pinned on the chain. Omit "
    "when this treasury holds exactly one tracked NFT there."))
_ACCOUNT = Field(None, pattern=r"^0x[0-9a-fA-F]{40}$", description=(
    "Or name the NFT by its token-bound account address."))


class RevokeAllParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chain: Literal["robinhood", "robinhood-testnet"] = _CHAIN
    nft: Optional[str] = _NFT
    account: Optional[str] = _ACCOUNT
    max_spend_usd: float = Field(2.0, gt=0, description="Most USD of FEE authorized for the whole sweep.")
    dry_run: bool = _DRY


class MintParams(BaseModel):
    """``mint(to, qty, minPnlOutPerToken)`` on a pinned collection profile with the ``mint``
    capability (``core/wallet/collection_registry.py``)."""
    model_config = ConfigDict(extra="forbid")
    to: Optional[str] = Field(None, pattern=r"^0x[0-9a-fA-F]{40}$", description=(
        "Recipient. Omit (default) = this instance's treasury, the only recipient accepted: the "
        "guard must see every minted token arrive in the paying wallet."))
    qty: int = Field(1, ge=1, le=10, description="How many to mint in one call (1..10); the price is qty × 0.042 ETH.")
    min_pnl_out: int = Field(0, ge=0, description=(
        "Least PNL (raw token units) each new account must receive from the mint's buy, "
        "else the mint reverts. 0 = never revert on price (the contract still caps impact at 3%)."))
    chain: Literal["robinhood", "robinhood-testnet"] = Field(
        "robinhood", description="'robinhood' (4663) or 'robinhood-testnet' (46630, no PNL pool: ETH fallback).")
    collection: Optional[str] = Field(None, pattern=r"^0x[0-9a-fA-F]{40}$", description=(
        "Only when more than one collection is pinned on the chain; it must be one of them."))
    max_spend_usd: float = Field(..., gt=0, description="Most USD authorized: qty × the mint price plus gas.")
    dry_run: bool = _DRY


class TakeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: str = Field(..., pattern=r"^0x[0-9a-fA-F]{40}$", description=(
        "Where the NFT goes (its account goes with it): a plain wallet. Refused: a token-bound "
        "account of a pinned collection, deployed or not yet minted (the token could own "
        "itself), and this treasury."))
    chain: Literal["robinhood", "robinhood-testnet"] = _CHAIN
    nft: Optional[str] = _NFT
    account: Optional[str] = _ACCOUNT
    max_spend_usd: float = Field(2.0, gt=0, description="Most USD of FEE authorized.")
    dry_run: bool = _DRY


class RevealParams(BaseModel):
    """``reveal(uint256[] ids)`` on a pinned collection profile with the ``reveal`` capability."""
    model_config = ConfigDict(extra="forbid")
    chain: Literal["robinhood", "robinhood-testnet"] = Field(
        "robinhood", description="'robinhood' (4663) or 'robinhood-testnet' (46630).")
    collection: Optional[str] = Field(None, pattern=r"^0x[0-9a-fA-F]{40}$", description=(
        "Only when more than one collection is pinned on the chain; it must be one of them."))
    max_ids: int = Field(10, ge=1, le=64, description=(
        "Most due ids revealed in one transaction, in id order (default 10: ~370k gas)."))
    max_spend_usd: Optional[float] = Field(None, gt=0, description=(
        "Most USD of gas FEE authorized; never above AGENT_NFT_REVEAL_MAX_GAS_USD (the default)."))
    dry_run: bool = _DRY


class AgentNftTool(WalletHolderMixin, BaseTool):
    """An agent NFT this instance owns: act FROM its ERC-6551 token-bound account as the NFT's owner."""

    def __init__(self, name: str = "agent_nft", config=None, container=None, *,
                 wallet=None, guard_fn=None, rpc_fn=None, impl=None, price_fn=None,
                 rail_factory=None):
        super().__init__(name=name, config=config if config is not None else _NULL_CONFIG,
                         container=container)
        self._wallet = wallet
        self._guard_fn = guard_fn
        self._rpc_fn = rpc_fn
        self._impl_override = impl
        self._price_fn = price_fn
        self._rail_factory = rail_factory

    async def _initialize(self):
        return True

    # -- plumbing the package uses ------------------------------------------

    def rpc_for(self, chain: str):
        """``callable(method, params)`` on the chain's pinned RPC."""
        if self._rpc_fn is not None:
            return self._rpc_fn
        from core.wallet.onchain import _rpc, rpc_url_for_chain
        url = rpc_url_for_chain(chain)
        return lambda method, params: _rpc(url, method, params, timeout=15.0)

    def guard(self, intent, tx, **kw):
        """THE authorizer. The package never imports tx_guard itself."""
        if self._guard_fn is not None:
            return self._guard_fn(intent, tx, **kw)
        from core.wallet import tx_guard
        return tx_guard.authorize(intent, tx, **kw)

    def _impl(self):
        if self._impl_override is not None:
            return self._impl_override
        # The names are core.tool_capabilities.AGENT_NFT_PACKAGE_MODULES, in that order.
        try:
            from polyrob_drop import verbs
        except ImportError:
            try:
                from polyrob_desk import verbs  # legacy package name until the drop repo renames
            except ImportError:
                return None
        return verbs

    def _gate(self, execution_context, verb: str, *, write: bool, dry_run: bool = True,
              entry: bool = True, needs_package: bool = True):
        if not agent_nft_enabled():
            return f"agent NFTs are off — set {FLAG}=true to arm them. Nothing was broadcast."
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
        if needs_package and self._impl() is None:
            return ("the agent-NFT package is not installed — install the `polyrob_drop` package "
                    "(not on PyPI; no `polyrob[...]` extra carries it). Nothing was broadcast.")
        return None

    async def _run(self, verb: str, params, execution_context, *, write: bool):
        err = self._gate(execution_context, verb, write=write,
                         dry_run=getattr(params, "dry_run", True))
        if err:
            return self._ar(error=err)
        fn = getattr(self._impl(), verb, None)
        if fn is None:
            return self._ar(error=f"the installed agent-NFT package has no `{verb}` — upgrade the agent-NFT package")
        try:
            return await fn(self, params, execution_context)
        except Exception as exc:  # noqa: BLE001 — a verb failure is an error result, never a crash
            logger.warning("agent_nft %s failed", verb, exc_info=True)
            return self._ar(error=f"agent_nft {verb} failed: {exc}. Nothing was broadcast unless a tx hash is shown.")

    # -- reads ----------------------------------------------------------------

    @BaseTool.action(
        "The agent NFT account this instance owns: every OPEN APPROVAL found and whether that table is "
        "complete, the purchase checks, owner (must be this treasury), lock and lockedUntil, state, "
        "identity, reveal state, native balance, journal entry count and head. Read-only.",
        param_model=SnapshotParams)
    async def agent_nft_snapshot(self, params: SnapshotParams, execution_context=None):
        return await self._run("snapshot", params, execution_context, write=False)

    @BaseTool.action(
        "Inspect ANY agent NFT before buying or taking it: open approvals FIRST with their coverage "
        "(complete or incomplete, never 'empty' when incomplete), then named checks (locked, approval "
        "table, lockedUntil, revealed), owner, state, identity count, native balance. Read-only.",
        param_model=InspectParams)
    async def agent_nft_inspect(self, params: InspectParams, execution_context=None):
        return await self._run("inspect", params, execution_context, write=False)

    # -- writes (every one through tx_guard with via_account) ---------------------

    @BaseTool.action(
        "Record a signed, chained journal entry (thesis or note) for the account of an NFT this "
        "instance owns; optionally anchor the head on-chain through the account (fee only).",
        param_model=JournalParams)
    async def agent_nft_journal(self, params: JournalParams, execution_context=None):
        return await self._run("journal", params, execution_context, write=True)

    @BaseTool.action(
        "Register the owned NFT account's ERC-8004 identity THROUGH the account (agentWallet = the "
        "account). Refuses if the account already holds one. Fee only.",
        param_model=BindParams)
    async def agent_nft_bind_identity(self, params: BindParams, execution_context=None):
        return await self._run("bind", params, execution_context, write=True)

    @BaseTool.action(
        "Revoke the open approvals the approval scan finds on the owned NFT account (ERC-20 "
        "allowances, ApprovalForAll grants, single-token NFT approvals, Permit2 allowances), through "
        "the account, one guarded call each — do this before the NFT leaves. NOT revoked: anything "
        "the scan does not find (an approval table reported incomplete stays incomplete). Fee only.",
        param_model=RevokeAllParams)
    async def agent_nft_revoke_all(self, params: RevokeAllParams, execution_context=None):
        # Core-owned body (069 v4 §5 rule 5: the guard names this verb as the remedy, so it
        # must not depend on the optional package). Risk-reducing: entry=False.
        return await self._core("revoke_all", params, execution_context, entry=False)

    @BaseTool.action(
        "Mint 1-10 tokens of a pinned collection to this instance's treasury (the treasury then "
        "owns them and their accounts) at the collection's price (0.042 ETH each). ALWAYS "
        "owner-approved.",
        param_model=MintParams)
    async def agent_nft_collection_mint(self, params: MintParams, execution_context=None):
        if params.collection and agent_nft_enabled():
            from core.wallet import collection_mint
            try:
                pinned = collection_mint.pinned_collections(params.chain)
            except Exception as exc:  # noqa: BLE001 — an untrusted registry pins nothing
                return self._ar(error=f"the collection registry cannot be trusted ({exc}). "
                                      f"Nothing was broadcast.")
            if params.collection.lower() not in pinned:
                return self._ar(error=(f"{params.collection} is not a pinned collection on "
                                       f"{params.chain}. Nothing was broadcast."))
        return await self._run("mint", params, execution_context, write=True)

    @BaseTool.action(
        "Send an agent NFT this treasury owns to an address `to` (its account goes with it). "
        "Refused while its account has ANY open approval — run agent_nft_revoke_all first. "
        "ALWAYS owner-approved.",
        param_model=TakeParams)
    async def agent_nft_withdraw_token(self, params: TakeParams, execution_context=None):
        # Core-owned body (069 v4 A4): the owner's `/nft send <id> <to>` runs it.
        return await self._core("withdraw", params, execution_context, entry=False)

    async def _core(self, verb: str, params, execution_context, *, entry: bool):
        err = self._gate(execution_context, verb, write=True, dry_run=params.dry_run,
                         entry=entry, needs_package=False)
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
        "Reveal every DUE id on a pinned collection: reveal(ids) from the treasury, "
        "in id order, up to max_ids per call. Spends gas only (no value, no asset; at most "
        "AGENT_NFT_REVEAL_MAX_GAS_USD of fee); the guard requires one Revealed/Recommitted per id "
        "and no other movement. Nothing due = no transaction.",
        param_model=RevealParams)
    async def agent_nft_collection_reveal(self, params: RevealParams, execution_context=None):
        # Core-owned body (tools/agent_nft/reveal.py): no agent-NFT package needed, and a
        # reveal opens no position, so the scoped `/pause trading` does not hold it (the
        # full owner pause does — here and again in tx_guard).
        err = self._gate(execution_context, "reveal", write=True, dry_run=params.dry_run,
                         entry=False, needs_package=False)
        if err:
            return self._ar(error=err)
        from tools.agent_nft import reveal
        try:
            return await reveal.run(self, params, execution_context)
        except Exception as exc:  # noqa: BLE001 — a verb failure is an error result, never a crash
            logger.warning("agent_nft collection reveal failed", exc_info=True)
            return self._ar(error=f"agent_nft collection reveal failed: {exc}. Nothing was broadcast unless a tx "
                                  f"hash is shown.")
