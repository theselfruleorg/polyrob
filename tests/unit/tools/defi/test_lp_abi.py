"""Selectors are DERIVED from the shapes; these literals catch a typo in a type."""
from core.wallet import abi
from tools.defi import lp_abi as A


def _sel(spec):
    return abi.selector(abi.signature_of(spec["name"], spec["inputs"]))


def test_npm_selectors_match_the_deployed_contract():
    assert _sel(A.NPM_MINT) == "0x88316456"
    assert _sel(A.NPM_INCREASE) == "0x219f5d17"
    assert _sel(A.NPM_DECREASE) == "0x0c49ccbe"
    assert _sel(A.NPM_COLLECT) == "0xfc6f7865"
    assert _sel(A.NPM_BURN) == "0x42966c68"
    assert _sel(A.NPM_MULTICALL) == "0xac9650d8"
    assert _sel(A.NPM_CREATE_AND_INIT) == "0x13ead562"
    assert _sel(A.NPM_REFUND_ETH) == "0x12210e8a"
    assert _sel(A.NPM_POSITIONS) == "0x99fbab88"


def test_pool_and_factory_selectors():
    assert _sel(A.FACTORY_GET_POOL) == "0x1698ee82"
    assert _sel(A.POOL_SLOT0) == "0x3850c7bd"
    assert _sel(A.POOL_LIQUIDITY) == "0x1a686502"


def test_event_topics():
    from eth_utils import keccak
    assert A.TOPIC_POOL_CREATED == "0x" + keccak(text="PoolCreated(address,address,uint24,int24,address)").hex()
    assert A.TOPIC_INCREASE_LIQUIDITY == "0x" + keccak(text="IncreaseLiquidity(uint256,uint128,uint256,uint256)").hex()
    assert A.TOPIC_DECREASE_LIQUIDITY == "0x" + keccak(text="DecreaseLiquidity(uint256,uint128,uint256,uint256)").hex()
    assert A.TOPIC_COLLECT == "0x" + keccak(text="Collect(uint256,address,uint256,uint256)").hex()


def test_mint_params_round_trip_encodes():
    data = abi.encode_call(A.NPM_MINT["name"], A.NPM_MINT["inputs"], [(
        "0x0000000000000000000000000000000000000001", "0x0000000000000000000000000000000000000002",
        3000, -887220, 887220, 10 ** 18, 2 * 10 ** 18, 0, 0,
        "0x0000000000000000000000000000000000000003", 1_800_000_000)])
    assert data.startswith("0x88316456") and len(data) == 2 + 8 + 11 * 64


def test_fee_tiers_to_spacing():
    assert A.FEE_TIERS == {100: 1, 500: 10, 3000: 60, 10000: 200}
