"""The ``defi_trade`` verbs' parameter models.

Split out of ``trade_tool.py`` (W9). ``trade_tool`` re-exports every name here,
so ``from tools.defi.trade_tool import SwapParams`` keeps working.
"""
from decimal import Decimal
from typing import Annotated, Literal, Optional

from pydantic import (BaseModel, BeforeValidator, Field, PlainSerializer,
                      WithJsonSchema)

from tools.defi.account_mode import ACCOUNT_DESC as _ACCOUNT_DESC, NFT_DESC as _NFT_DESC

#: 068 B2: ONE chain-name fold at the verb boundary — stripped, lowercase, the
#: same fold ``chains.get`` applies. Without it `BASE` reached the chain registry
#: (case-insensitive) but missed every canonical pin and tracked position keyed
#: by `base`, and a fake USDC bought as `BASE` passed the identity gate.
ChainName = Annotated[str, BeforeValidator(
    lambda v: v.strip().lower() if isinstance(v, str) else v)]


def _exact_supply(value):
    """085 W7: a token supply parses EXACTLY — a string or an int as-is, a JSON
    number through ``repr`` and only when it has 15 significant digits or fewer.
    A ``float`` field rounded 123456789.123456789 before any check saw it."""
    from core.wallet.token_template import to_exact_supply
    return to_exact_supply(value)


def _supply_out(value) -> str:
    from core.wallet.token_template import format_supply
    return format_supply(value)


#: The ONE supply type for both deploy verbs.
#:
#: ⚠️ The schema is a plain ``string``, not ``anyOf [number, string]``: the
#: Gemini converter (``modules/llm/gemini_client.py`` ``dict_to_schema``) reads
#: only ``type`` and drops ``anyOf``, which left the property TYPELESS. A
#: string is the exact form anyway, and the validator still takes a JSON number
#: from a model that sends one (no provider here runs strict tool schemas).
#: The serializer dumps the exact plain digits, so ``model_dump()`` stays
#: ``json.dumps``-safe (history files, cards) and 1e9 and 1000000000 dump and
#: hash the same.
ExactSupply = Annotated[
    Decimal, BeforeValidator(_exact_supply),
    PlainSerializer(_supply_out, return_type=str, when_used="always"),
    WithJsonSchema({"type": "string"})]


def _chain_field_description(verb: str) -> str:
    from core.wallet import chains
    return (f"Chain to {verb} on — CHOOSE IT DELIBERATELY, because the same "
            f"address is a different token on a different chain. Chains that "
            f"can move value: {', '.join(chains.money_chains())}. Gas differs "
            f"by orders of magnitude, so a cent-scale trade can cost more in "
            f"gas on ethereum than it is worth, while base is a fraction of a "
            f"cent. Full guidance:\n{chains.chain_guidance()}")


class TransferParams(BaseModel):
    chain: ChainName = Field("base", description=_chain_field_description("send"))
    token: str = Field(..., description=(
        "CONTRACT ADDRESS of the token to send (0x…), or 'native' to send the "
        "chain's gas asset itself (ETH on ethereum/base/arbitrum/robinhood, POL "
        "on polygon). A token ticker is not accepted — resolve it to an address "
        "with defi_data.token_resolve first."))
    to: str = Field(..., description="Recipient address (0x…)")
    amount: float = Field(..., gt=0, description="Human amount to send (e.g. 0.25)")
    max_spend_usd: float = Field(..., gt=0, description=(
        "The most USD you authorize for this transfer. Asserted against the "
        "SIMULATED outflow — if the simulation disagrees, the transfer is refused."))
    dry_run: bool = Field(True, description=(
        "TRUE (default) simulates and returns the guard's verdict without "
        "broadcasting. Set false to actually move funds."))
    send_anyway: bool = Field(False, description=(
        "Only for a token whose value is NOT VERIFIED (no spend-grade price). "
        "It sends only on the owner's own turn; your call goes to the owner."))
    account: Optional[str] = Field(None, description=_ACCOUNT_DESC)
    nft: Optional[str] = Field(None, description=_NFT_DESC)


class ApproveParams(BaseModel):
    chain: ChainName = Field("base", description=_chain_field_description("approve on"))
    token: str = Field(..., description="CONTRACT ADDRESS of the token to approve (0x…)")
    spender: str = Field(..., description="Address being granted the allowance (0x…)")
    amount: float = Field(..., gt=0, description=(
        "EXACT human amount to approve. Unlimited approvals are refused — "
        "approve only what this trade needs, then revoke."))
    max_spend_usd: float = Field(..., gt=0, description=(
        "The most USD this approval may put at risk, asserted against the "
        "simulated allowance grant."))
    via: Literal["erc20", "permit2"] = Field("erc20", description=(
        "'erc20' (default): token.approve(spender). 'permit2': an exact Permit2 "
        "grant, expiring in 15 min, to the pinned Uniswap v4 PositionManager "
        "only — the step before lp_add(protocol='v4')."))
    dry_run: bool = Field(True, description="TRUE simulates only. Set false to sign.")


class RevokeParams(BaseModel):
    chain: ChainName = Field("base", description=_chain_field_description("revoke on"))
    token: str = Field(..., description="CONTRACT ADDRESS of the token (0x…)")
    spender: str = Field(..., description="Address whose allowance is set to ZERO (0x…)")
    dry_run: bool = Field(True, description="TRUE simulates only. Set false to sign.")


class NftTransferParams(BaseModel):
    chain: ChainName = Field(description=_chain_field_description("nft_transfer"))
    contract: str = Field(description="The NFT collection contract address.")
    token_id: int = Field(ge=0, description="The token id to send.")
    to: str = Field(description="Recipient address. This is irreversible.")
    standard: str = Field(
        default="erc721",
        description="erc721 (unique token) or erc1155 (semi-fungible). Say which "
                    "— guessing would encode a call the contract may misread.")
    amount: int = Field(default=1, ge=1,
                        description="Quantity, erc1155 only. An erc721 is unique, "
                                    "so its amount must be 1.")
    max_spend_usd: float = Field(
        default=25.0, gt=0,
        description="Ceiling for the transaction FEE. ⚠️ It does NOT bound the "
                    "value of what you are sending — an NFT has no reliable "
                    "price, which is why this verb always needs owner approval.")
    dry_run: bool = True


class RegisterAgentParams(BaseModel):
    chain: ChainName = Field(default="base",
                       description="Chain whose ERC-8004 Identity Registry to "
                                   "register on. Pinned; unsupported chains refuse.")
    max_spend_usd: float = Field(default=25.0, gt=0,
                                 description="Ceiling for the transaction FEE. "
                                             "A registration sends nothing else.")
    dry_run: bool = True


class SetAgentUriParams(BaseModel):
    chain: ChainName = Field(default="base")
    agent_id: int = Field(..., ge=0, description="Your existing agentId (tokenId).")
    max_spend_usd: float = Field(default=10.0, gt=0)
    dry_run: bool = True


class NftRevokeParams(BaseModel):
    chain: ChainName = Field(description=_chain_field_description("nft_revoke_approval"))
    contract: str = Field(description="The NFT collection contract address.")
    operator: str = Field(
        description="The operator whose blanket approval over this collection "
                    "is retired. This can only REDUCE risk.")
    max_spend_usd: float = Field(default=5.0, gt=0)
    dry_run: bool = True


class WrapParams(BaseModel):
    chain: ChainName = Field("base", description=_chain_field_description("wrap native on"))
    amount: float = Field(..., gt=0, description=(
        "Human amount of NATIVE to wrap (e.g. 0.0012 ETH -> 0.0012 WETH)."))
    max_spend_usd: float = Field(..., gt=0, description=(
        "The most USD you authorize to leave the wallet, asserted against the "
        "SIMULATED native outflow."))
    dry_run: bool = Field(True, description=(
        "TRUE (default) simulates and returns the guard's verdict without "
        "broadcasting. Set false to actually wrap."))


class UnwrapParams(BaseModel):
    chain: ChainName = Field("base", description=_chain_field_description("unwrap on"))
    amount: float = Field(..., gt=0, description=(
        "Human amount of WRAPPED native to turn back into gas "
        "(e.g. 0.0012 WETH -> 0.0012 ETH)."))
    max_spend_usd: float = Field(..., gt=0, description=(
        "The most USD you authorize to leave the wallet, asserted against the "
        "SIMULATED wrapped-native outflow."))
    dry_run: bool = Field(True, description=(
        "TRUE (default) simulates and returns the guard's verdict without "
        "broadcasting. Set false to actually unwrap."))


class SwapParams(BaseModel):
    chain: ChainName = Field("base", description=_chain_field_description("swap on"))
    token_in: str = Field(..., description=(
        "CONTRACT ADDRESS of the token you are selling (0x…), or the literal "
        "'native' to spend the chain's gas asset (ETH/SOL/etc) directly. A "
        "ticker is not accepted — resolve it with defi_data.token_resolve first. "
        "PREFER 'native' when the wallet holds the gas asset: it needs NO "
        "allowance (so no approve_token, no extra transaction, no extra gas) "
        "and on Robinhood it is the cheapest entry there is."))
    token_out: str = Field(..., description="CONTRACT ADDRESS of the token you are buying (0x…)")
    amount_in: float = Field(..., gt=0, description="Human amount of token_in to sell")
    max_spend_usd: float = Field(..., gt=0, description=(
        "The most USD you authorize to leave the wallet, asserted against the "
        "SIMULATED outflow."))
    minimum_output_raw: Optional[int] = Field(None, ge=1, le=2**256-1,
        description="Minimum raw output confirmed by the owner; every new route must preserve this floor.")
    slippage_bps: Optional[int] = Field(None, ge=1, le=1000, description=(
        "Max slippage in basis points (100 = 1%). Defaults to "
        "DEFI_MAX_SLIPPAGE_BPS. Bounds amountOutMinimum, so the swap reverts "
        "on-chain rather than filling at a worse price."))
    dry_run: bool = Field(True, description=(
        "TRUE (default) quotes and simulates without broadcasting. Set false "
        "to actually trade."))
    account: Optional[str] = Field(None, description=_ACCOUNT_DESC)
    nft: Optional[str] = Field(None, description=_NFT_DESC)


class SolanaSwapParams(BaseModel):
    token_in: str = Field(..., description="MINT ADDRESS of the token to sell (base58)")
    token_out: str = Field(..., description="MINT ADDRESS of the token to buy (base58)")
    amount_in: float = Field(..., gt=0, description="Human amount of token_in to sell")
    max_spend_usd: float = Field(..., gt=0, description=(
        "Your declared ceiling for this swap in USD. The guard holds you to it."))
    minimum_output_raw: Optional[int] = Field(None, ge=1, le=2**256-1,
        description="Minimum raw output confirmed by the owner; every new route must preserve this floor.")
    slippage_bps: Optional[int] = Field(None, ge=1, le=1000, description=(
        "Slippage bound in basis points. Jupiter bakes its own minimum into the "
        "transaction it builds, so this is a bar that minimum must CLEAR — a "
        "looser route is refused, not rewritten."))
    dry_run: bool = Field(True, description=(
        "TRUE (default) simulates and asserts without broadcasting."))


class DeployTokenParams(BaseModel):
    chain: ChainName = Field("base", description=_chain_field_description("deploy on"))
    name: str = Field(..., description=(
        "Token name, e.g. 'Rob Coin'. Max 64 characters, printable ASCII only."))
    symbol: str = Field(..., description=(
        "Ticker, e.g. 'ROB'. Max 16 characters, printable ASCII only."))
    supply: ExactSupply = Field(..., gt=0, description=(
        "TOTAL supply in WHOLE units, minted in full to the agent wallet. There "
        "is no mint function, so this is the supply forever. A string of digits, "
        "e.g. \"1000000000\" (1e9 and 1,000,000,000 also parse)."))
    decimals: int = Field(18, ge=0, le=18, description=(
        "Decimal places. 18 is the ERC-20 norm and what every DEX assumes."))
    max_spend_usd: float = Field(..., gt=0, description=(
        "The most USD this deployment may cost. A token deploy sends nothing, so "
        "this bounds the GAS FEE — on an L2 that is cents."))
    # CR-H07: kept as fields so a call that sets them gets the verb's refusal
    # by name, but no longer advertised as a feature.
    salt: str = Field("", description=(
        "Leave EMPTY. A salt is REFUSED for deploy_token: through the CREATE2 "
        "factory the constructor would mint the whole supply to the factory, "
        "not the wallet, and it would be lost."))
    vanity: str = Field("", description=(
        "Leave EMPTY. A vanity prefix is REFUSED for deploy_token for the same "
        "reason as salt: the supply would be minted to the CREATE2 factory."))
    dry_run: bool = Field(True, description=(
        "TRUE (default) simulates, asserts the produced bytecode against the "
        "pinned template and reports the address it WOULD land at, without "
        "broadcasting. Set false to actually deploy."))


class DeployContractParams(BaseModel):
    chain: ChainName = Field("base", description=_chain_field_description("deploy on"))
    bytecode: str = Field(..., description=(
        "COMPILED creation bytecode (init code), 0x-prefixed hex. NOT Solidity "
        "source — nothing here compiles. To mint an ordinary fixed-supply token, "
        "use deploy_token instead: it needs no bytecode and its runtime is "
        "asserted against an audited template."))
    constructor_args: str = Field("", description=(
        "ABI-encoded constructor arguments, appended to the bytecode. Leave "
        "empty when the constructor takes none."))
    value: float = Field(0.0, ge=0, description=(
        "NATIVE value to endow the constructor with, in whole coin units (not "
        "wei). Usually 0."))
    max_spend_usd: float = Field(..., gt=0, description=(
        "The most USD this deployment may cost — endowment plus worst-case fee."))
    salt: str = Field("", description=(
        "Optional 32-byte hex salt. Set it (or `vanity`) to deploy through the "
        "canonical CREATE2 factory, which gives the token the SAME ADDRESS ON "
        "EVERY CHAIN for the same bytes. Leave empty for an ordinary "
        "nonce-based deploy."))
    vanity: str = Field("", description=(
        "Optional HEX prefix to mine the address for, e.g. 'b0b' or 'dead' "
        "(0-9a-f only, max 8 characters — each character is 16x the work). "
        "Implies the CREATE2 path."))
    dry_run: bool = Field(True, description=(
        "TRUE (default) simulates and reports without broadcasting."))


class SolanaDeployTokenParams(BaseModel):
    name: str = Field("", description=(
        "Token name, e.g. 'Rob Coin'. Written ON-CHAIN via Token-2022 metadata, "
        "so every explorer and wallet shows it. Defaults to the symbol. Max 64 "
        "characters, printable ASCII only."))
    symbol: str = Field(..., description=(
        "Ticker, e.g. 'ROB'. Written ON-CHAIN alongside the name. Max 16 "
        "characters, printable ASCII only."))
    uri: str = Field("", description=(
        "Optional https:// or ipfs:// URI of a JSON metadata file "
        "({name, symbol, description, image}). This is where a LOGO comes "
        "from — without it the token shows with no picture anywhere. Host it "
        "yourself; any public HTTPS URL works."))
    supply: ExactSupply = Field(..., gt=0, description=(
        "TOTAL supply in WHOLE units (a string is exact), minted in full to the agent's Solana "
        "token account. The mint authority is revoked in the same transaction, "
        "so this is the supply forever."))
    decimals: int = Field(9, ge=0, le=9, description=(
        "Decimal places. 9 is Solana's convention (SOL itself). supply x "
        "10**decimals must fit in a u64."))
    max_spend_usd: float = Field(..., gt=0, description=(
        "The most USD this may cost. A mint creation sends nothing — the cost "
        "is ~0.0035 SOL of rent for the mint and the token account, plus fees."))
    dry_run: bool = Field(True, description=(
        "TRUE (default) builds, simulates and asserts the whole supply arrives, "
        "without broadcasting."))


class LpAddParams(BaseModel):
    chain: ChainName = "robinhood"
    protocol: str = Field("v3", description=(
        "'v3', or 'v4' for a new FULL-RANGE position on a Pons graduated pool "
        "(token_a/token_b = 'native' + the Pons token; needs an ERC-20 allowance "
        "to Permit2 and approve_token(via='permit2') first; capped by LP_ETH_CAP)."))
    token_a: str = Field(..., description="Contract address or 'native'.")
    token_b: str = Field(..., description="Contract address or 'native'.")
    amount_a: float = Field(..., ge=0, allow_inf_nan=False,
                            description="Most of token_a to deposit, in whole token units (not raw).")
    amount_b: float = Field(..., ge=0, allow_inf_nan=False,
                            description="Most of token_b to deposit, in whole token units (not raw).")
    fee: int = Field(3000, description="Millionths: 100, 500, 3000 or 10000. 3000 = 0.3%.")
    range: str = Field("full", description="full, low,high prices in token_b/token_a, or ticks:low,high in sorted token order.")
    initial_price: Optional[float] = Field(None, gt=0, allow_inf_nan=False, description=(
        "Only to CREATE a pool that does not exist yet: the starting price as token_b per "
        "token_a. Omit for an existing pool."))
    token_id: Optional[int] = Field(None, ge=0, description=(
        "Your existing position NFT id to ADD to. Omit to mint a new position."))
    # CR-M12: capped at the swap rail's bound — LP minimums are a slippage
    # floor too, and 50% let a sandwich take half the deposit.
    slippage_bps: int = Field(100, ge=0, le=1000, description="Deposit minimums, in basis points (max 1000).")
    fragment: bool = Field(False, description=(
        "Set true ONLY to knowingly open a separate pool for a Pons token still on its "
        "bonding curve (that fragments its market); otherwise such a deposit is refused."))
    max_spend_usd: float = Field(..., gt=0, allow_inf_nan=False,
                                 description="The most USD both legs plus gas may cost.")
    dry_run: bool = Field(True, description="TRUE (default) simulates and asserts without broadcasting.")


class LpCollectParams(BaseModel):
    chain: ChainName = "robinhood"
    protocol: str = Field("v3", description="'v3' only.")
    token_id: int = Field(..., ge=0, description="Your position NFT id.")
    max_spend_usd: float = Field(5.0, gt=0, allow_inf_nan=False, description="Maximum gas fee in USD.")
    dry_run: bool = Field(True, description="TRUE (default) simulates and asserts without broadcasting.")


class LpRemoveParams(LpCollectParams):
    liquidity_pct: int = Field(100, ge=1, le=100, description="Percent of the position's liquidity to withdraw.")
    slippage_bps: int = Field(100, ge=0, le=1000,   # CR-M12: the swap bound
                              description="Withdrawal minimums, in basis points (max 1000).")
    burn: bool = Field(False, description="At 100% only: also burn the emptied position NFT.")


class CallParams(BaseModel):
    chain: ChainName = Field("base", description=_chain_field_description("call on"))
    to: str = Field(..., description="CONTRACT ADDRESS being called (0x…)")
    calldata: str = Field(..., description=(
        "ABI-encoded calldata, 0x-prefixed: the 4-byte selector followed by the "
        "encoded arguments. Build it yourself or take it from a protocol's own "
        "transaction-preparation endpoint."))
    value: float = Field(0.0, ge=0, description=(
        "NATIVE value to send with the call, in whole coin units (0.01 = 0.01 "
        "ETH, converted at 18 decimals) — NOT wei. Declare it — an undeclared "
        "native movement is refused."))
    spend_token: Optional[str] = Field(None, description=(
        "CONTRACT ADDRESS of the token this call SPENDS, or null when it spends "
        "native (or nothing). The simulated outflow is asserted against "
        "spend_max_raw."))
    spend_max_raw: int = Field(0, ge=0, description=(
        "The most RAW units of spend_token the call may move out. 0 with a "
        "spend_token declared means 'this call must not move that token'."))
    receive_token: Optional[str] = Field(None, description=(
        "CONTRACT ADDRESS of the token this call must RETURN, when it returns "
        "one. Required if receive_min_raw is set."))
    receive_min_raw: int = Field(0, ge=0, description=(
        "The MINIMUM raw units of receive_token the simulation must measure "
        "coming back. This is the assertion that makes arbitrary calldata safe "
        "to sign. Leave 0 only for a call that genuinely receives nothing: at 0 "
        "NO return is checked and the guard only bounds what leaves."))
    allow_spender: Optional[str] = Field(None, description=(
        "Address this call is EXPECTED to grant an allowance to, if any. An "
        "undeclared allowance grant is always refused."))
    allow_max_raw: int = Field(0, ge=0, description=(
        "The most raw units allow_spender may be granted."))
    max_spend_usd: float = Field(..., gt=0, description=(
        "The most USD you authorize to leave the wallet on this call."))
    dry_run: bool = Field(True, description=(
        "TRUE (default) simulates and asserts without broadcasting."))


class BridgeParams(BaseModel):
    from_chain: ChainName = Field(..., description=(
        "Origin chain NAME: 'solana', or a registry name (base, robinhood, …)."))
    to_chain: ChainName = Field(..., description="Destination chain NAME.")
    amount: float = Field(..., gt=0, description=(
        "Human amount of the ORIGIN asset (`token_in`): SOL/ETH for native, "
        "dollars for usdc."))
    token_in: str = Field("native", description=(
        "ORIGIN asset: 'native' (default) or 'usdc' = the origin chain's PINNED "
        "USDC. On an EVM chain usdc adds an exact-amount approve to the pinned "
        "Relay depository. Any other token is refused."))
    token_out: str = Field("native", description=(
        "DESTINATION asset: 'native' (default), or a token PINNED in the chain "
        "registry for the destination chain — 'weth' or 'usdc' where that chain "
        "has one. An arbitrary address is refused: every pinned address was "
        "verified on-chain, a supplied one was not. On Robinhood, native ETH is "
        "both gas AND the asset that routes straight into a position, so 'native' "
        "is usually the better answer than 'weth'."))
    dry_run: bool = Field(True, description=(
        "TRUE (default) quotes, asserts and simulates without broadcasting."))
