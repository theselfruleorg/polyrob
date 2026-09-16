"""The ``launchpad`` tool — launch a token, and trade one on its bonding curve.

⚠️ This module deliberately does NOT ``from __future__ import annotations``: the
Registry introspects each action's first-parameter annotation to route the
validated param model, and stringized annotations break that.

⚠️ **Everything a launchpad lists is a memecoin on a bonding curve.** The rails
here bound what can be SPENT and assert what is RECEIVED; nothing here makes a
launch or a buy a good idea, and the tool ships default-OFF and out of every
default toolset.
"""
import logging
import types
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from tools.base_tool import BaseTool

logger = logging.getLogger(__name__)

FLAG = "LAUNCHPAD_ENABLED"

#: Slippage bound on a curve trade, in basis points. The curve enforces the
#: min-out ON-CHAIN, so this is a real bound, not an estimate.
DEFAULT_SLIPPAGE_BPS = 300

#: A tool constructed for a test carries no BotConfig. Same sentinel the defi
#: tool uses, for the same reason: the verbs read nothing off it.
_NULL_CONFIG = types.SimpleNamespace()


def launchpad_enabled() -> bool:
    from core.env import bool_env
    return bool_env(FLAG, False)


class LaunchParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., description="Token name, e.g. 'Rob Coin'.")
    symbol: str = Field(..., description="Ticker, e.g. 'ROB'.")
    description: str = Field("", description="Short description shown on the launchpad.")
    logo: str = Field("", description="Logo URI (ipfs://… or https://…).")
    twitter: str = Field("", description="Optional X/Twitter URL.")
    website: str = Field("", description="Optional website URL.")
    buy_amount: float = Field(0.0, ge=0, description=(
        "Opening buy in the quote asset (native ETH on Robinhood Chain). 0 "
        "launches WITHOUT an opening buy. An opening buy is how the creator "
        "takes a position before anyone else can; the launchpad's snipe tax "
        "exempts the deployer, so it is not taxed."))
    creator_tax_bps: int = Field(100, ge=0, le=1000, description=(
        "Creator tax on every curve trade, in basis points (100 = 1%). Paid to "
        "this wallet. The launchpad's own maximum is 1000."))
    slippage_bps: int = Field(DEFAULT_SLIPPAGE_BPS, ge=1, le=5000, description=(
        "Slippage bound on the opening buy. Enforced on-chain by the curve."))
    max_spend_usd: float = Field(..., gt=0, description=(
        "The most USD you authorize to leave the wallet — the launch fee plus "
        "the opening buy plus gas."))
    dry_run: bool = Field(True, description=(
        "TRUE (default) reads the live terms, prices the buy and reports what "
        "WOULD happen without broadcasting."))


class TradeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(..., description=(
        "CONTRACT ADDRESS of the launchpad token (0x…). The curve is looked up "
        "from the factory's own record, never supplied."))
    amount: float = Field(..., gt=0, description=(
        "BUY: how much of the quote asset to spend. SELL: how many tokens to sell."))
    slippage_bps: int = Field(DEFAULT_SLIPPAGE_BPS, ge=1, le=5000,
                              description="Slippage bound, enforced on-chain.")
    max_spend_usd: float = Field(..., gt=0, description=(
        "The most USD you authorize to leave the wallet."))
    dry_run: bool = Field(True, description="TRUE (default) simulates only.")


class QuoteParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(..., description="CONTRACT ADDRESS of the launchpad token.")
    side: str = Field("buy", description="'buy' or 'sell'.")
    amount: float = Field(..., gt=0, description=(
        "BUY: quote asset in. SELL: tokens in."))


class StatusParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(..., description="CONTRACT ADDRESS of the launchpad token.")


class ClaimParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(..., description=(
        "CONTRACT ADDRESS of a token YOU launched. Everything else is read on "
        "chain: the curve from the factory's launch record, the fee escrow from "
        "the curve, the amount from the escrow. There is no parameter that can "
        "aim this at another contract."))
    max_spend_usd: float = Field(1.0, gt=0, description=(
        "The most USD of FEE you authorize. A claim sends nothing, so this "
        "bounds the gas, not the amount received."))
    dry_run: bool = Field(True, description=(
        "TRUE (default) simulates and reports what would arrive, broadcasting "
        "nothing. Set false to actually claim."))


def _fmt_native(wei) -> str:
    """Wei as a native amount, EXACTLY. `unknown` when it could not be read — a
    balance nobody reported is not a balance of zero.

    Decimal, not float: `13613005871733144000 / 10**18` renders
    13.613005871733143692, which is a different number than the chain holds. A
    money figure that drifts in the last digits is the kind of thing that gets
    reconciled against an explorer and read as a discrepancy.
    """
    if wei is None:
        return "unknown"
    from decimal import Decimal
    text = format(Decimal(int(wei)) / Decimal(10 ** 18), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _metadata_block(params) -> str:
    """What the launch is about to write about itself, and what it is not.

    ⚠️ These fields are CONSTRUCTOR ARGUMENTS — they are committed with the
    token and there is no setter. A logo URL with a typo in it is permanent, so
    the quote has to SHOW the strings rather than just accept them; the owner
    cannot check afterwards.

    The absent-logo nudge lives here, not at a seat, because all three callers
    (Telegram, the CLI and the agent's own action) render this one header. The
    first cut put it in the Telegram helper only, which is the same shape of
    miss as a chat verb that joins three of its four lists.
    """
    rows = [("logo", params.logo), ("description", params.description),
            ("x", params.twitter), ("site", params.website)]
    present = [f"  {k+':':<13}{v}\n" for k, v in rows if (v or "").strip()]
    missing = [k for k, v in rows if not (v or "").strip()]
    out = "".join(present)
    if not (params.logo or "").strip():
        out += ("  ⚠️ NO LOGO. On a launchpad that is how a token looks "
                "abandoned, and it is most of what a buyer sees before "
                "anything else.\n")
    if missing:
        out += f"  not set:     {', '.join(missing)} — permanent once launched\n"
    return out


class LaunchpadTool(BaseTool):
    """Pons V2 on Robinhood Chain. One provider today; the seam takes more."""

    def __init__(self, name: str = "launchpad", config=None, container=None, *,
                 wallet=None, rail_factory=None, guard_fn=None, price_fn=None,
                 fallback_price_fn=None, rpc_fn=None):
        super().__init__(name=name,
                         config=config if config is not None else _NULL_CONFIG,
                         container=container)
        self._wallet = wallet
        self._rail_factory = rail_factory
        self._guard_fn = guard_fn
        self._price_fn = price_fn
        self._fallback_price_fn = fallback_price_fn
        self._rpc_fn = rpc_fn

    async def _initialize(self):
        # A missing wallet is a per-verb error with a remedy, never an init
        # failure — the read verbs work without one.
        return True

    # -- plumbing ---------------------------------------------------------

    def _ar(self, *, content: str = None, error: str = None):
        from tools.controller.types import ActionResult
        if error is not None:
            return ActionResult(error=error)
        return ActionResult(extracted_content=content)

    def _get_wallet(self):
        if self._wallet is not None:
            return self._wallet
        from core.wallet.factory import get_agent_wallet
        return get_agent_wallet()

    def _price(self, chain, addr):
        if self._price_fn:
            return self._price_fn(chain, addr)
        from tools.defi.providers import dexscreener
        return dexscreener.token(chain, addr).price_usd

    def _fallback_price(self, chain, addr):
        if self._fallback_price_fn:
            return self._fallback_price_fn(chain, addr)
        return None

    def _rpc(self, method, params):
        if self._rpc_fn is not None:
            return self._rpc_fn(method, params)
        from core.wallet.onchain import _rpc, rpc_url_for_chain
        from tools.launchpad import pons_abi as P
        return _rpc(rpc_url_for_chain(P.CHAIN), method, params, timeout=10.0)

    def _notify_tx(self, execution_context, notice, *, settled: bool) -> None:
        try:
            from core.wallet import tx_notify
            tx_notify.notify_soon(
                getattr(self, "container", None),
                getattr(execution_context, "user_id", None), notice,
                settled=settled,
                session_id=getattr(execution_context, "session_id", None))
        except Exception:
            logger.debug("launchpad: owner notice failed", exc_info=True)

    def _preflight(self, execution_context, verb: str, *, dry_run: bool):
        """Flag, turn origin and the 031 pause. None when clear."""
        if not launchpad_enabled():
            return (f"the launchpad is off — set {FLAG}=true to arm it. Nothing "
                    f"was broadcast.")
        from tools.defi.deploy_verb import _refuse_non_owner_turn, _refuse_paused
        turn_err = _refuse_non_owner_turn(execution_context, verb)
        if turn_err:
            return turn_err
        if not dry_run:
            paused = _refuse_paused()
            if paused:
                return paused + " RESULT: NOT SENT."
        return None

    def _signer_address(self):
        wallet = self._get_wallet()
        return wallet.operational_signer().address if wallet else None

    # -- read verbs -------------------------------------------------------

    @BaseTool.action(
        "Read a launchpad token's live state: its bonding curve, reserves, how "
        "far it is from graduating to a locked Uniswap pool, its fees, and "
        "whether the snipe tax is still active. Read-only, costs nothing.",
        param_model=StatusParams)
    async def launchpad_status(self, params: StatusParams, execution_context=None):
        from tools.launchpad import pons, pons_abi as P
        if not launchpad_enabled():
            return self._ar(error=f"the launchpad is off — set {FLAG}=true to arm it.")
        try:
            record = pons.launched_token(self._rpc, params.token)
        except Exception as exc:
            return self._ar(error=f"could not read the factory record: {exc}")
        if record is None:
            return self._ar(content=(
                f"{params.token} was not launched by Pons on {P.CHAIN}. It may "
                f"be an ordinary token, or from another launchpad — this tool "
                f"can only speak for its own factory's records."))
        who = self._signer_address() or P.NATIVE_PAIR
        try:
            state = pons.curve_state(self._rpc, record["curve"], recipient=who)
        except Exception as exc:
            return self._ar(error=f"could not read the curve: {exc}")

        threshold = int(record["graduationThreshold"])
        progress = (100.0 * state.quote_reserve / threshold) if threshold else 0.0
        native = record["pairToken"].lower() == P.NATIVE_PAIR.lower()
        pool_line = ""
        if state.graduated:
            from tools.defi.lp_reads import pons_pool_key, pool_id
            try:
                key = pons_pool_key(record)
                pool_line = (f"  graduated pool: {pool_id(key)}; PoolKey={key}\n"
                             "  LP fee 0 — the hook collects; creator income is claimed via escrow. "
                             "The launch locker position cannot be withdrawn.\n")
            except Exception as exc:
                pool_line = f"  graduated pool: unavailable ({exc})\n"
        return self._ar(content=(
            f"{params.token} on {P.CHAIN} (Pons V2)\n"
            f"  curve:       {record['curve']}\n"
            f"  deployer:    {record['deployer']}\n"
            f"  quoted in:   {'native ETH' if native else record['pairToken']}\n"
            f"  reserves:    {state.quote_reserve} quote / {state.token_reserve} token\n"
            f"  graduation:  {progress:.1f}% of {threshold} "
            f"({'GRADUATED' if state.graduated else 'on the curve'})\n"
            f"  fees:        {state.fee_bps} bps curve + "
            f"{state.creator_tax_bps} bps creator\n"
            f"  snipe tax:   {state.snipe_tax_bps} bps for this wallet"
            f"{'  ⚠️ STILL ACTIVE — a buy now loses this much' if state.snipe_tax_bps else ''}\n"
            + pool_line + self._claimable_line(record["curve"], who)))

    def _claimable_line(self, curve: str, who: Optional[str]) -> str:
        """What the launchpad is holding FOR US, said out loud.

        This line is the reason 13.613 ETH sat unreachable for days: status
        reported reserves, graduation and fee bps, so nothing ever mentioned
        that money had accrued, and the agent's own probes hit the curve (where
        the balance is always 0) instead of the escrow.
        """
        from tools.launchpad import pons
        if not who:
            return ("  claimable:   unknown — no wallet is configured, so there "
                    "is no address to ask the escrow about\n")
        try:
            owed = pons.claimable(self._rpc, curve, holder=who)
        except Exception as exc:
            return (f"  claimable:   unknown — the fee escrow could not be read "
                    f"({exc}). Unknown is not zero.\n")
        line = (f"  claimable:   {_fmt_native(owed.native_wei)} native to this "
                f"wallet, in escrow {owed.escrow}\n"
                f"               ⚠ this is the escrow's balance for the WALLET, "
                f"across EVERY token it launched — not just this token. One "
                f"claim collects all of it.\n")
        if owed.unswept_wei:
            line += (f"               + {_fmt_native(owed.unswept_wei)} not yet "
                     f"swept from the curve into the escrow (not claimable yet)\n")
        if owed.native_wei:
            line += "               run launchpad_claim to take it\n"
        return line

    @BaseTool.action(
        "CLAIM the creator fees a token you launched has earned. The launchpad "
        "credits your 1% creator tax to a fee escrow, not to the curve and not "
        "to your wallet — it sits there until you claim it. The escrow credits "
        "an ADDRESS, so ONE claim collects what every token this wallet "
        "launched has earned; naming a token here only tells the tool which "
        "curve to read the escrow address from. Sends nothing: the only cost "
        "is gas, and the simulation must prove the money arrives before "
        "anything is broadcast.",
        param_model=ClaimParams)
    async def launchpad_claim(self, params: ClaimParams, execution_context=None):
        from tools.launchpad import execute, pons, pons_abi as P
        if not launchpad_enabled():
            return self._ar(error=f"the launchpad is off — set {FLAG}=true to arm it.")
        who = self._signer_address()
        if who is None:
            return self._ar(error="agent wallet not enabled (AGENT_WALLET_ENABLED)")
        try:
            # The pins are the ROOT of this verb's provenance chain: the curve
            # comes from the factory's record, the escrow from the curve, the
            # amount from the escrow. If the factory is not the code that was
            # reviewed, none of those three links means anything.
            pons.verify_pins(self._rpc)
        except Exception as exc:
            return self._ar(error=f"refusing to claim: {exc}")
        try:
            record = pons.launched_token(self._rpc, params.token)
        except Exception as exc:
            return self._ar(error=f"could not read the factory record: {exc}")
        if record is None:
            return self._ar(content=(
                f"{params.token} was not launched by Pons on {P.CHAIN}, so this "
                f"tool has no fees to claim for it."))
        try:
            owed = pons.claimable(self._rpc, record["curve"], holder=who)
        except Exception as exc:
            # An unreadable escrow is NOT an empty one. Claiming against a
            # balance we could not read would declare a minimum we invented.
            return self._ar(error=(
                f"could not read the fee escrow for {params.token}: {exc}. "
                f"Nothing was broadcast, and this is not evidence that the "
                f"balance is zero."))
        if owed.native_wei <= 0:
            unswept = (f" {_fmt_native(owed.unswept_wei)} native is accrued on the "
                       f"curve but not yet swept into the escrow."
                       if owed.unswept_wei else "")
            return self._ar(content=(
                f"nothing to claim for {params.token}.\n"
                f"  escrow {owed.escrow} owes this wallet 0.{unswept}\n"
                f"  Nothing was broadcast."))

        built = pons.build_claim(escrow=owed.escrow)
        header = (
            f"claim creator fees for {params.token} on {P.CHAIN} (Pons V2)\n"
            f"  curve:     {record['curve']}\n"
            f"  escrow:    {owed.escrow}  (read FROM the curve, not supplied)\n"
            f"  recipient: {who}\n"
            f"  claiming:  {_fmt_native(owed.native_wei)} native, the full balance "
            f"the escrow reports owing this wallet\n"
            f"  ⚠ scope:   the escrow credits an ADDRESS, not a token. This "
            f"collects what is owed for EVERY token this wallet launched, so "
            f"there is no second claim to make afterwards.\n")
        return await execute.guarded_send(
            self, execution_context=execution_context, verb="claim",
            chain=P.CHAIN, to=built["to"], calldata=built["calldata"],
            value_wei=0, max_spend_usd=params.max_spend_usd,
            dry_run=params.dry_run, header=header,
            is_claim=True, min_native_inflow_wei=owed.native_wei)

    @BaseTool.action(
        "Price a buy or sell on a launchpad bonding curve BEFORE committing. "
        "Returns the exact amount out and every fee leg. Read-only.",
        param_model=QuoteParams)
    async def launchpad_quote(self, params: QuoteParams, execution_context=None):
        from tools.launchpad import pons
        if not launchpad_enabled():
            return self._ar(error=f"the launchpad is off — set {FLAG}=true to arm it.")
        side = (params.side or "buy").strip().lower()
        if side not in ("buy", "sell"):
            return self._ar(error="side must be 'buy' or 'sell'")
        state, record, err = self._resolve_curve(params.token)
        if err:
            return self._ar(error=err)

        decimals = 18
        try:
            if side == "buy":
                raw_in = int(round(params.amount * 10 ** decimals))
                out, legs = pons.quote_buy(state, raw_in)
                body = (f"  spend:  {params.amount:g} quote ({raw_in} raw)\n"
                        f"  RECEIVE: {out} raw tokens\n")
            else:
                raw_in = int(round(params.amount * 10 ** 18))
                out, legs = pons.quote_sell(state, raw_in)
                body = (f"  sell:   {params.amount:g} tokens ({raw_in} raw)\n"
                        f"  RECEIVE: {out} raw quote\n")
        except pons.PonsError as exc:
            return self._ar(error=f"refused: {exc}")

        return self._ar(content=(
            f"{side} quote on {params.token} (Pons V2, curve {state.curve})\n"
            + body
            + "".join(f"  {k}: {v}\n" for k, v in legs.items())
            + (f"  ⚠️ the snipe tax is still active at {state.snipe_tax_bps} bps\n"
               if state.snipe_tax_bps else "")))

    # -- write verbs ------------------------------------------------------

    @BaseTool.action(
        "LAUNCH a new token on the Pons launchpad (Robinhood Chain): deploys "
        "the token and its bonding curve in one transaction, with an optional "
        "opening buy. The launch terms (fee, supply, graduation threshold) are "
        "read LIVE and committed, so terms that move between the quote and the "
        "broadcast revert instead of silently applying. ⚠️ Everything on a "
        "launchpad is a memecoin; this makes launching possible, not wise. "
        "dry_run defaults to TRUE.",
        param_model=LaunchParams)
    async def launchpad_launch(self, params: LaunchParams, execution_context=None):
        from tools.launchpad import execute, pons, pons_abi as P

        err = self._preflight(execution_context, "launch a token",
                              dry_run=params.dry_run)
        if err:
            return self._ar(error=err)
        wallet = self._get_wallet()
        if wallet is None:
            return self._ar(error="agent wallet not enabled (AGENT_WALLET_ENABLED)")
        creator = wallet.operational_signer().address

        try:
            pons.verify_pins(self._rpc)
            terms = pons.read_terms(self._rpc)
        except Exception as exc:
            return self._ar(error=f"refused: {exc}")
        if not terms.enabled:
            return self._ar(error=(
                "the Pons factory has launches DISABLED right now — nothing "
                "was broadcast."))

        quote_in = int(round(params.buy_amount * 10 ** terms.pair_decimals))
        socials = (params.twitter, "", "", params.website, "")
        try:
            if quote_in > 0:
                min_out = self._opening_min_out(terms, quote_in, params.slippage_bps)
                built = pons.build_launch_and_buy(
                    terms=terms, name=params.name, symbol=params.symbol,
                    creator=creator, recipient=creator, quote_in=quote_in,
                    min_tokens_out=min_out, logo=params.logo,
                    description=params.description, socials=socials,
                    creator_tax_bps=params.creator_tax_bps)
            else:
                min_out = 0
                built = pons.build_launch(
                    terms=terms, name=params.name, symbol=params.symbol,
                    creator=creator, logo=params.logo,
                    description=params.description, socials=socials,
                    creator_tax_bps=params.creator_tax_bps)
        except pons.PonsError as exc:
            return self._ar(error=f"refused: {exc}")

        header = (
            f"launch {params.symbol} ({params.name}) on Pons V2 / {P.CHAIN}\n"
            f"  supply:      {terms.supply_raw} raw (fixed by the launchpad)\n"
            f"  launch fee:  {terms.launch_fee_wei} wei\n"
            f"  opening buy: "
            f"{f'{params.buy_amount:g} quote, min {min_out} raw tokens out' if quote_in else 'none'}\n"
            f"  graduates:   at {terms.graduation_threshold} raw quote in the curve\n"
            f"  creator tax: {params.creator_tax_bps} bps to {creator}\n"
            f"  economics:   committed {terms.economics[:18]}…\n"
            + _metadata_block(params))

        def _on_receipt(rail, tx_hash):
            receipt = execute.receipt_logs(rail, tx_hash) or {}
            found = pons.parse_token_launched(receipt.get("logs"))
            if not found:
                return ("  ⚠️ the transaction confirmed but no TokenLaunched "
                        "event was found in its receipt — check the explorer "
                        "before telling anyone an address.\n"), None
            return (f"  token: {found['token']}\n"
                    f"  curve: {found['curve']}\n"), found

        return await execute.guarded_send(
            self, execution_context=execution_context, verb="launch",
            chain=P.CHAIN, to=built["to"], calldata=built["calldata"],
            value_wei=built["value_wei"], max_spend_usd=params.max_spend_usd,
            dry_run=params.dry_run, header=header, on_receipt=_on_receipt)

    @BaseTool.action(
        "BUY a launchpad token on its bonding curve. The curve is resolved from "
        "the factory's own record (never a supplied address), the price is "
        "computed exactly and the minimum out is enforced ON-CHAIN. Refuses "
        "while the launch snipe tax is still active — that tax opens at 99%. "
        "dry_run defaults to TRUE.",
        param_model=TradeParams)
    async def launchpad_buy(self, params: TradeParams, execution_context=None):
        from tools.launchpad import execute, pons, pons_abi as P

        err = self._preflight(execution_context, "buy on a launchpad",
                              dry_run=params.dry_run)
        if err:
            return self._ar(error=err)
        state, record, err = self._resolve_curve(params.token)
        if err:
            return self._ar(error=err)
        if state.graduated:
            return self._ar(error=(
                f"{params.token} has GRADUATED off the bonding curve to a "
                f"Uniswap pool — buy it with defi_trade.swap, not here."))
        if state.snipe_tax_bps > pons.MAX_TOLERATED_SNIPE_BPS:
            return self._ar(error=(
                f"refused: the launch snipe tax on {params.token} is still "
                f"{state.snipe_tax_bps} bps for this wallet. It decays to zero "
                f"within seconds of launch — wait, then buy. Nothing was "
                f"broadcast."))

        native = record["pairToken"].lower() == P.NATIVE_PAIR.lower()
        if not native:
            return self._ar(error=(
                f"{params.token} is quoted in {record['pairToken']}, not native. "
                f"An ERC-20-quoted buy needs an allowance leg to the curve "
                f"first — use defi_trade.approve_token, then defi_trade.call."))

        quote_in = int(round(params.amount * 10 ** 18))
        try:
            expected, legs = pons.quote_buy(state, quote_in)
        except pons.PonsError as exc:
            return self._ar(error=f"refused: {exc}")
        min_out = expected * (10_000 - params.slippage_bps) // 10_000
        if min_out <= 0:
            return self._ar(error="refused: the priced output rounds to zero")

        built = pons.build_buy(curve=state.curve, quote_in=quote_in,
                               min_tokens_out=min_out,
                               recipient=self._signer_address(),
                               native_quote=True)
        header = (
            f"buy {params.amount:g} native of {params.token} on its Pons curve\n"
            f"  curve:    {state.curve}\n"
            f"  expected: {expected} raw tokens (min {min_out} enforced on-chain)\n"
            f"  fees:     {legs['fee']} curve + {legs['creator_tax']} creator"
            f" + {legs['snipe_tax']} snipe\n")

        return await execute.guarded_send(
            self, execution_context=execution_context, verb="buy",
            chain=P.CHAIN, to=built["to"], calldata=built["calldata"],
            value_wei=built["value_wei"], max_spend_usd=params.max_spend_usd,
            dry_run=params.dry_run, header=header,
            receive_token=state.token, min_inflow_raw=min_out)

    @BaseTool.action(
        "SELL a launchpad token back into its bonding curve. Needs no "
        "allowance — a Pons token grants its own curve one. The minimum "
        "received is enforced on-chain AND asserted against the simulation. "
        "dry_run defaults to TRUE.",
        param_model=TradeParams)
    async def launchpad_sell(self, params: TradeParams, execution_context=None):
        from tools.launchpad import execute, pons, pons_abi as P

        err = self._preflight(execution_context, "sell on a launchpad",
                              dry_run=params.dry_run)
        if err:
            return self._ar(error=err)
        state, record, err = self._resolve_curve(params.token)
        if err:
            return self._ar(error=err)
        if state.graduated:
            return self._ar(error=(
                f"{params.token} has GRADUATED off the bonding curve — sell it "
                f"with defi_trade.swap, not here."))

        native = record["pairToken"].lower() == P.NATIVE_PAIR.lower()
        tokens_in = int(round(params.amount * 10 ** 18))
        try:
            expected, legs = pons.quote_sell(state, tokens_in)
        except pons.PonsError as exc:
            return self._ar(error=f"refused: {exc}")
        min_out = expected * (10_000 - params.slippage_bps) // 10_000
        if min_out <= 0:
            return self._ar(error=(
                "refused: the priced proceeds round to zero — this position is "
                "worth nothing on the curve at this size"))

        built = pons.build_sell(curve=state.curve, tokens_in=tokens_in,
                                min_quote_out=min_out,
                                recipient=self._signer_address())
        header = (
            f"sell {params.amount:g} of {params.token} into its Pons curve\n"
            f"  curve:    {state.curve}\n"
            f"  expected: {expected} raw quote (min {min_out} enforced on-chain)\n"
            f"  fees:     {legs['fee']} curve + {legs['creator_tax']} creator\n")

        return await execute.guarded_send(
            self, execution_context=execution_context, verb="sell",
            chain=P.CHAIN, to=built["to"], calldata=built["calldata"],
            value_wei=0, max_spend_usd=params.max_spend_usd,
            dry_run=params.dry_run, header=header,
            spend_token=state.token, spend_max_raw=tokens_in,
            watch_spenders=(state.curve,),
            # A native-quoted curve pays NATIVE, which rule 6b would otherwise
            # refuse as an undeclared movement. Declaring the minimum turns that
            # blanket refusal into an assertion (042).
            min_native_inflow_wei=(min_out if native else None),
            receive_token=(None if native else record["pairToken"]),
            min_inflow_raw=(None if native else min_out))

    # -- shared -----------------------------------------------------------

    def _resolve_curve(self, token: str):
        """``(state, record, error)`` — the curve comes from the FACTORY only."""
        from tools.launchpad import pons, pons_abi as P
        try:
            pons.verify_pins(self._rpc)
            record = pons.launched_token(self._rpc, token)
        except Exception as exc:
            return None, None, f"refused: {exc}"
        if record is None:
            return None, None, (
                f"{token} is not a Pons-launched token on {P.CHAIN} — the "
                f"factory has no record of it. The curve address is taken from "
                f"that record and from nowhere else, so there is nothing to "
                f"trade against here.")
        who = self._signer_address() or record["deployer"]
        try:
            state = pons.curve_state(self._rpc, record["curve"], recipient=who)
        except Exception as exc:
            return None, None, f"refused: could not read the curve ({exc})"
        return state, record, None

    def _opening_min_out(self, terms, quote_in: int, slippage_bps: int) -> int:
        """Min-out for the OPENING buy, priced off the launchpad's own config.

        The curve does not exist yet, so its reserves are exactly the config's
        ``phantomQuote`` and ``supply`` — which is what makes an opening buy
        priceable at all. The fee legs floor separately, as they do everywhere
        else on this curve.
        """
        fee = quote_in * terms.curve_fee_bps // 10_000
        net = quote_in - fee
        if net <= 0:
            return 0
        tokens = terms.supply_raw * net // (terms.phantom_quote + net)
        return max(0, tokens * (10_000 - slippage_bps) // 10_000)
