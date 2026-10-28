"""The ``defi_data`` verbs' parameter models.

Split out of ``data_tool.py`` (W9). ``data_tool`` re-exports every name here,
so ``from tools.defi.data_tool import PortfolioParams`` keeps working.
"""
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field


def _chain_field(what: str) -> str:
    from core.wallet import chains
    return (f"Chain to {what} — the same address is a DIFFERENT token on a "
            f"different chain, so name it deliberately. Readable: "
            f"{', '.join(chains.names())}.")


class ResolveParams(BaseModel):
    symbol: str = Field(..., description=(
        "Ticker to look up. Returns CANDIDATES ranked by liquidity — never a "
        "single answer. You must then choose a contract address yourself; "
        "several different contracts commonly claim the same ticker."))


class SwapQuoteParams(BaseModel):
    chain: str = Field(..., description=_chain_field("quote on"))
    token_in: str = Field(..., description="CONTRACT ADDRESS of the token to sell (0x…), or the literal 'native' (quoted via the chain's wrapped-native, as the swap verb prefers)")
    token_out: str = Field(..., description="CONTRACT ADDRESS of the token to buy (0x…), or the literal 'native'")
    amount_in: float = Field(..., gt=0, description="Human amount of token_in to sell")


class TokenRefParams(BaseModel):
    chain: str = Field(..., description=_chain_field("look the token up on"))
    address: str = Field(..., description=(
        "The TOKEN's address: on EVM chains its contract (0x…, 40 hex); on "
        "solana its MINT (base58). A ticker is not accepted — use token_resolve "
        "first. A WALLET is not a token: to see what a wallet holds use "
        "wallet_holdings."))


class PortfolioParams(BaseModel):
    chain: str = Field(..., description=_chain_field("report holdings on"))
    account: Optional[str] = Field(None, description=(
        "Optional (069 v4) — report the holdings of the token-bound ERC-6551 account of an NFT "
        "this treasury OWNS instead of the treasury's (a pinned collection only; needs "
        "AGENT_NFT_ENABLED)."))
    nft: Optional[str] = Field(None, description=(
        "Optional — the same as `account`, naming the NFT: '<collection>#<id>' or '<id>'."))


def chain_required_in_schema(schema: dict) -> None:
    """Provider-facing schema only: list ``chain`` as required.

    ``chain`` is optional in Python (a base58 address needs none, and in-process
    callers rely on that) but REQUIRED for a 0x address. A model reads an optional
    parameter as skippable: prod 2026-10-26 sent ``{"address": "0x…"}`` with no chain
    on every call, twice in one morning, through the error text that named the fix.
    Required parameters are the ones models actually fill in.
    """
    required = schema.setdefault("required", [])
    if "chain" not in required:
        required.append("chain")



class WalletHoldingsParams(BaseModel):
    model_config = ConfigDict(json_schema_extra=chain_required_in_schema)
    address: str = Field(..., description=(
        "The WALLET address to read — any owner, not only this agent's. A Solana "
        "wallet is base58 (e.g. 7xKX…); an EVM wallet is 0x… (40 hex). Give a "
        "wallet, not a token: for a token contract/mint use token_info."))
    chain: Optional[str] = Field(None, description=(
        "Chain to read. Omit for a base58 address (it is read on solana). "
        "REQUIRED for a 0x address — the same 0x wallet exists on every EVM "
        "chain with different holdings. " + _chain_field("read the wallet on")))


class ReconcileParams(BaseModel):
    chain: str = Field(..., description=_chain_field("reconcile against"))
    account: Optional[str] = Field(None, description=(
        "Reconcile a token-bound ERC-6551 account (its 0x address) instead of the "
        "treasury wallet. Omit for the treasury."))


class PositionsParams(BaseModel):
    """071 W3: the rail book, with the arithmetic done."""
    chain: Optional[str] = Field(None, description=(
        "Optional — one chain. Omit for every chain the rail book tracks."))


class DiscoverParams(BaseModel):
    chain: str = Field(..., description=_chain_field("discover pools on"))
    limit: int = Field(20, ge=1, le=50, description="How many pools to list.")
    min_liquidity_usd: float = Field(0.0, ge=0.0, description=(
        "Exclude pools whose reported liquidity is BELOW this. A pool whose "
        "liquidity is UNKNOWN is never excluded by this floor — unknown is not "
        "a number, and dropping it would hide it from you silently."))
    min_volume_h24_usd: float = Field(0.0, ge=0.0, description=(
        "Exclude pools whose 24h volume is below this. Same unknown rule."))


class OhlcvParams(BaseModel):
    chain: str = Field(..., description=_chain_field("read candles on"))
    address: str = Field(..., description=(
        "The TOKEN contract address. Candles are POOL-scoped on this indexer, so "
        "the deepest pool for this token is resolved and named in the answer."))
    pool: Optional[str] = Field(None, description=(
        "Read this exact POOL instead of resolving one. Use it when you already "
        "know which pool you mean — a token's pools disagree."))
    timeframe: str = Field("hour", description=(
        "Candle window: 'day', 'hour' or 'minute'. Use 'day' to see whether a "
        "token climbed over weeks, 'hour' to see whether it went vertical."))
    aggregate: int = Field(1, ge=1, le=60, description=(
        "Bars to merge per candle, e.g. timeframe='minute' aggregate=15."))
    limit: int = Field(48, ge=1, le=300, description="How many candles to return.")


class ScanParams(BaseModel):
    chain: str = Field(..., description=_chain_field("scan pools on"))
    kind: str = Field("trending", description=(
        "'trending' (what the indexer currently ranks) or 'new' (the "
        "fresh-launch frontier). Trending is a popularity claim and popularity "
        "is purchasable; new is unproven by definition."))
    limit: int = Field(20, ge=1, le=50, description="How many pools to classify.")
    min_liquidity_usd: float = Field(0.0, ge=0.0, description=(
        "Exclude pools whose reported liquidity is BELOW this. A pool whose "
        "liquidity is UNKNOWN is never excluded — unknown is not a number."))


class ContractReadParams(BaseModel):
    chain: str = Field(..., description=_chain_field("read from"))
    address: str = Field(..., description="Contract address to read from (0x…). EVM chains only — there is no eth_call on solana.")
    signature: str = Field(..., description="Function signature, e.g. 'totalSupply()'")
    args: List[Any] = Field(default_factory=list, description="Arguments (currently unencoded; prefer no-arg views)")


class NftHoldingsParams(BaseModel):
    chain: str = Field(..., description=_chain_field("list NFT holdings on"))
    address: Optional[str] = Field(
        None, description=("Wallet address to inspect (0x…). EVM chains only. "
                           "Defaults to this agent's own wallet."))


class NftInfoParams(BaseModel):
    chain: str = Field(..., description=_chain_field("read the token on"))
    contract: str = Field(..., description="The NFT collection contract (0x…). EVM chains only.")
    token_id: int = Field(..., ge=0, description="The token id to read.")


class LpPositionsParams(BaseModel):
    chain: str = Field(..., description=_chain_field("read Uniswap v3 positions on"))
    protocol: str = Field("v3", description="Uniswap protocol: v3 (v4 in a later phase).")
    address: Optional[str] = Field(None, description="Owner address; defaults to the agent wallet.")


class LpPoolInfoParams(BaseModel):
    chain: str = Field(..., description=_chain_field("read a Uniswap v3 pool on"))
    protocol: str = Field("v3", description="v3, or v4 for a Pons graduated pool (give token_a + token_b; the other side is 'native').")
    pool: Optional[str] = Field(None, description="Pool address, OR give token_a+token_b+fee.")
    token_a: Optional[str] = None
    token_b: Optional[str] = None
    fee: Optional[int] = Field(None, description="Fee tier in hundredths of a bip: 100, 500, 3000, 10000.")


class PoolMetricsParams(BaseModel):
    chain: str = Field(..., description=_chain_field("read a Pons v4 pool on"))
    token: str = Field(..., description="The Pons token whose graduated v4 pool to read (0x…). EVM only — Pons is a Robinhood-chain launchpad.")
    expect_pool_id: Optional[str] = Field(None, description=(
        "The pool id you expect (bytes32). A different PoolKey hash is reported as an ALERT."))
    depth_pct: List[float] = Field(default_factory=lambda: [1.0, 2.0, 5.0], description=(
        "Price moves (in %) to size the depth for, both directions."))
    mint_native: float = Field(0.00825, gt=0, description="Native size of one buy, for the impact figure.")
    hook_fee_bps: int = Field(100, ge=0, le=10000, description=(
        "Fee the hook takes from the input, in bps (090: 1%). An assumption, not read."))


class LpQuoteParams(BaseModel):
    chain: str = Field(..., description=_chain_field("quote a Uniswap v3 deposit on"))
    protocol: str = Field("v3", description="v3, or v4 for a Pons graduated pool (full range only; the other side is 'native').")
    token_a: str
    token_b: str
    fee: int = Field(3000, description="100 | 500 | 3000 | 10000")
    amount_a: Optional[float] = Field(None, description="Human units of token_a to deposit (give one or both).")
    amount_b: Optional[float] = None
    range: str = Field("full", description="'full' or 'price_lo,price_hi' in token_b per token_a.")
    initial_price: Optional[float] = Field(
        None, description="token_b per token_a — REQUIRED when the pool does not exist yet.")
