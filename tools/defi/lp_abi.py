"""Uniswap v3 periphery/core ABI shapes for the liquidity rail. Selectors are
derived from these shapes by `core/wallet/abi.py`, never pasted; the tests pin the
resulting 4-bytes so a wrong type string cannot ship."""
from eth_utils import keccak

MAX_UINT128 = 2 ** 128 - 1
#: fee (hundredths of a bip) -> tickSpacing, as enabled on every Uniswap v3 factory.
FEE_TIERS = {100: 1, 500: 10, 3000: 60, 10000: 200}


def _t(sig: str) -> str:
    return "0x" + keccak(text=sig).hex()


TOPIC_POOL_CREATED = _t("PoolCreated(address,address,uint24,int24,address)")
TOPIC_INCREASE_LIQUIDITY = _t("IncreaseLiquidity(uint256,uint128,uint256,uint256)")
TOPIC_DECREASE_LIQUIDITY = _t("DecreaseLiquidity(uint256,uint128,uint256,uint256)")
TOPIC_COLLECT = _t("Collect(uint256,address,uint256,uint256)")

_MINT_PARAMS = {"name": "params", "type": "tuple", "components": [
    {"name": "token0", "type": "address"}, {"name": "token1", "type": "address"},
    {"name": "fee", "type": "uint24"},
    {"name": "tickLower", "type": "int24"}, {"name": "tickUpper", "type": "int24"},
    {"name": "amount0Desired", "type": "uint256"}, {"name": "amount1Desired", "type": "uint256"},
    {"name": "amount0Min", "type": "uint256"}, {"name": "amount1Min", "type": "uint256"},
    {"name": "recipient", "type": "address"}, {"name": "deadline", "type": "uint256"}]}
NPM_MINT = {"name": "mint", "inputs": [_MINT_PARAMS], "outputs": [
    {"name": "tokenId", "type": "uint256"}, {"name": "liquidity", "type": "uint128"},
    {"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}]}
NPM_INCREASE = {"name": "increaseLiquidity", "inputs": [{"name": "params", "type": "tuple", "components": [
    {"name": "tokenId", "type": "uint256"},
    {"name": "amount0Desired", "type": "uint256"}, {"name": "amount1Desired", "type": "uint256"},
    {"name": "amount0Min", "type": "uint256"}, {"name": "amount1Min", "type": "uint256"},
    {"name": "deadline", "type": "uint256"}]}], "outputs": [
    {"name": "liquidity", "type": "uint128"}, {"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}]}
NPM_DECREASE = {"name": "decreaseLiquidity", "inputs": [{"name": "params", "type": "tuple", "components": [
    {"name": "tokenId", "type": "uint256"}, {"name": "liquidity", "type": "uint128"},
    {"name": "amount0Min", "type": "uint256"}, {"name": "amount1Min", "type": "uint256"},
    {"name": "deadline", "type": "uint256"}]}], "outputs": [
    {"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}]}
NPM_COLLECT = {"name": "collect", "inputs": [{"name": "params", "type": "tuple", "components": [
    {"name": "tokenId", "type": "uint256"}, {"name": "recipient", "type": "address"},
    {"name": "amount0Max", "type": "uint128"}, {"name": "amount1Max", "type": "uint128"}]}], "outputs": [
    {"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}]}
NPM_BURN = {"name": "burn", "inputs": [{"name": "tokenId", "type": "uint256"}], "outputs": []}
NPM_MULTICALL = {"name": "multicall", "inputs": [{"name": "data", "type": "bytes[]"}],
                 "outputs": [{"name": "results", "type": "bytes[]"}]}
NPM_CREATE_AND_INIT = {"name": "createAndInitializePoolIfNecessary", "inputs": [
    {"name": "token0", "type": "address"}, {"name": "token1", "type": "address"},
    {"name": "fee", "type": "uint24"}, {"name": "sqrtPriceX96", "type": "uint160"}],
    "outputs": [{"name": "pool", "type": "address"}]}
NPM_REFUND_ETH = {"name": "refundETH", "inputs": [], "outputs": []}
NPM_POSITIONS = {"name": "positions", "inputs": [{"name": "tokenId", "type": "uint256"}], "outputs": [
    {"name": "nonce", "type": "uint96"}, {"name": "operator", "type": "address"},
    {"name": "token0", "type": "address"}, {"name": "token1", "type": "address"},
    {"name": "fee", "type": "uint24"}, {"name": "tickLower", "type": "int24"}, {"name": "tickUpper", "type": "int24"},
    {"name": "liquidity", "type": "uint128"},
    {"name": "feeGrowthInside0LastX128", "type": "uint256"}, {"name": "feeGrowthInside1LastX128", "type": "uint256"},
    {"name": "tokensOwed0", "type": "uint128"}, {"name": "tokensOwed1", "type": "uint128"}]}
NPM_BALANCE_OF = {"name": "balanceOf", "inputs": [{"name": "owner", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]}
NPM_TOKEN_OF_OWNER_BY_INDEX = {"name": "tokenOfOwnerByIndex", "inputs": [
    {"name": "owner", "type": "address"}, {"name": "index", "type": "uint256"}], "outputs": [{"name": "", "type": "uint256"}]}
NPM_OWNER_OF = {"name": "ownerOf", "inputs": [{"name": "tokenId", "type": "uint256"}], "outputs": [{"name": "", "type": "address"}]}
FACTORY_GET_POOL = {"name": "getPool", "inputs": [
    {"name": "tokenA", "type": "address"}, {"name": "tokenB", "type": "address"}, {"name": "fee", "type": "uint24"}],
    "outputs": [{"name": "pool", "type": "address"}]}
POOL_SLOT0 = {"name": "slot0", "inputs": [], "outputs": [
    {"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"},
    {"name": "observationIndex", "type": "uint16"}, {"name": "observationCardinality", "type": "uint16"},
    {"name": "observationCardinalityNext", "type": "uint16"}, {"name": "feeProtocol", "type": "uint8"},
    {"name": "unlocked", "type": "bool"}]}
POOL_LIQUIDITY = {"name": "liquidity", "inputs": [], "outputs": [{"name": "", "type": "uint128"}]}
POOL_FEE_GROWTH_GLOBAL0 = {"name": "feeGrowthGlobal0X128", "inputs": [], "outputs": [{"name": "", "type": "uint256"}]}
POOL_FEE_GROWTH_GLOBAL1 = {"name": "feeGrowthGlobal1X128", "inputs": [], "outputs": [{"name": "", "type": "uint256"}]}
POOL_TICKS = {"name": "ticks", "inputs": [{"name": "tick", "type": "int24"}], "outputs": [
    {"name": "liquidityGross", "type": "uint128"}, {"name": "liquidityNet", "type": "int128"},
    {"name": "feeGrowthOutside0X128", "type": "uint256"}, {"name": "feeGrowthOutside1X128", "type": "uint256"},
    {"name": "tickCumulativeOutside", "type": "int56"}, {"name": "secondsPerLiquidityOutsideX128", "type": "uint160"},
    {"name": "secondsOutside", "type": "uint32"}, {"name": "initialized", "type": "bool"}]}
POOL_TOKEN0 = {"name": "token0", "inputs": [], "outputs": [{"name": "", "type": "address"}]}
POOL_TOKEN1 = {"name": "token1", "inputs": [], "outputs": [{"name": "", "type": "address"}]}
POOL_FEE = {"name": "fee", "inputs": [], "outputs": [{"name": "", "type": "uint24"}]}
POOL_TICK_SPACING = {"name": "tickSpacing", "inputs": [], "outputs": [{"name": "", "type": "int24"}]}
ERC20_ALLOWANCE = {"name": "allowance", "inputs": [
    {"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]}
ERC20_DECIMALS = {"name": "decimals", "inputs": [], "outputs": [{"name": "", "type": "uint8"}]}
