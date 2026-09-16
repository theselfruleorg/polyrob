import pytest
from core.wallet import abi
from tools.defi import lp_abi as A, lp_reads as R

NPM = "0x73991a25c818bf1f1128deaab1492d45638de0d3"
FACTORY = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
POOL = "0x00000000000000000000000000000000000000ab"
T0, T1 = "0x0000000000000000000000000000000000000010", "0x0000000000000000000000000000000000000020"


def _enc(outputs, values):
    return "0x" + abi.encode(outputs, values).hex()


class FakeRpc:
    def __init__(self): self.calls = {}
    def add(self, to, spec, values, args_prefix=None):
        sel = abi.selector(abi.signature_of(spec["name"], spec["inputs"]))
        self.calls[(to.lower(), sel)] = _enc(spec["outputs"], values)
    def __call__(self, method, params, timeout=8.0):
        assert method == "eth_call"
        to, data = params[0]["to"].lower(), params[0]["data"]
        return self.calls.get((to, data[:10]), "0x")


def test_pool_address_none_when_factory_returns_zero():
    rpc = FakeRpc(); rpc.add(FACTORY, A.FACTORY_GET_POOL, ["0x" + "0" * 40])
    assert R.pool_address(rpc, "robinhood", T0, T1, 3000) is None


def test_pool_state_reads_slot0_and_prices():
    rpc = FakeRpc()
    rpc.add(POOL, A.POOL_TOKEN0, [T0]); rpc.add(POOL, A.POOL_TOKEN1, [T1])
    rpc.add(POOL, A.POOL_FEE, [3000]); rpc.add(POOL, A.POOL_TICK_SPACING, [60])
    rpc.add(POOL, A.POOL_SLOT0, [2 ** 96, 0, 0, 1, 1, 0, True]); rpc.add(POOL, A.POOL_LIQUIDITY, [10 ** 18])
    rpc.add(T0, A.ERC20_DECIMALS, [18]); rpc.add(T1, A.ERC20_DECIMALS, [18])
    st = R.pool_state(rpc, "robinhood", POOL)
    assert st.tick == 0 and st.liquidity == 10 ** 18 and abs(st.price0_in_1 - 1.0) < 1e-9


def test_position_view_computes_amounts_and_in_range():
    rpc = FakeRpc()
    rpc.add(NPM, A.NPM_POSITIONS, [0, "0x" + "0" * 40, T0, T1, 3000, -60, 60, 10 ** 18, 0, 0, 5, 7])
    rpc.add(FACTORY, A.FACTORY_GET_POOL, [POOL])
    rpc.add(POOL, A.POOL_TOKEN0, [T0]); rpc.add(POOL, A.POOL_TOKEN1, [T1])
    rpc.add(POOL, A.POOL_FEE, [3000]); rpc.add(POOL, A.POOL_TICK_SPACING, [60])
    rpc.add(POOL, A.POOL_SLOT0, [2 ** 96, 0, 0, 1, 1, 0, True]); rpc.add(POOL, A.POOL_LIQUIDITY, [10 ** 18])
    rpc.add(POOL, A.POOL_FEE_GROWTH_GLOBAL0, [0]); rpc.add(POOL, A.POOL_FEE_GROWTH_GLOBAL1, [0])
    rpc.add(POOL, A.POOL_TICKS, [0, 0, 0, 0, 0, 0, 0, True])
    rpc.add(T0, A.ERC20_DECIMALS, [18]); rpc.add(T1, A.ERC20_DECIMALS, [18])
    pv = R.position(rpc, "robinhood", 42)
    assert pv.in_range and pv.amount0 > 0 and pv.amount1 > 0
    assert (pv.fees0, pv.fees1) == (5, 7)


def test_owned_position_ids_enumerates():
    rpc = FakeRpc(); rpc.add(NPM, A.NPM_BALANCE_OF, [2])
    # tokenOfOwnerByIndex differs by args; FakeRpc keys on selector only, so return the same id twice
    rpc.add(NPM, A.NPM_TOKEN_OF_OWNER_BY_INDEX, [7])
    assert R.owned_position_ids(rpc, "robinhood", "0x" + "1" * 40) == [7, 7]


def test_read_error_not_zero_on_empty():
    with pytest.raises(R.LpReadError):
        R.pool_state(FakeRpc(), "robinhood", POOL)


def test_pons_pool_key_and_id_for_a_native_pair():
    record = {"token": T1, "pairToken": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
              "poolFee": 0, "tickSpacing": 200}
    key = R.pons_pool_key(record, native_pair="0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73")
    assert key["currency0"] == "0x" + "0" * 40 and key["currency1"].lower() == T1
    assert key["hooks"].lower() == "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
    assert R.pool_id(key).startswith("0x") and len(R.pool_id(key)) == 66


def test_rpc_failure_is_a_read_error_not_an_empty_position_list():
    def fail(*a, **kw):
        raise RuntimeError('RPC offline')
    with pytest.raises(R.LpReadError, match='RPC offline'):
        R.owned_position_ids(fail, 'robinhood', T0)


def test_enumeration_limit_is_explicit_not_silently_truncated():
    rpc = FakeRpc(); rpc.add(NPM, A.NPM_BALANCE_OF, [201])
    with pytest.raises(R.LpReadError, match='enumeration limit'):
        R.owned_position_ids(rpc, 'robinhood', T0)
