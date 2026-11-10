"""The Uniswap deployments are PINNED. An env var may confirm one, never introduce one."""
import pytest
from core.wallet import dex_registry as R

def test_robinhood_v3_row_matches_the_verified_feed():
    row = R.row_for("robinhood", "v3")
    assert row.chain_id == 4663
    assert row.factory.lower() == "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
    assert row.position_manager.lower() == "0x73991a25c818bf1f1128deaab1492d45638de0d3"

def test_base_v4_row_matches_the_verified_feed():
    row = R.row_for("base", "v4")
    assert row.pool_manager.lower() == "0x498581ff718922c3f8e6a244956af099b2652b2b"
    assert row.position_manager.lower() == "0x7c5f5a4bbd8fd63184577525326123b519429bdc"
    assert row.permit2.lower() == "0x000000000022d473030f116ddee9f6b43ac78ba3"

def test_unknown_chain_or_protocol_is_none_not_a_guess():
    assert R.row_for("optimism", "v3") is None
    assert R.row_for("robinhood", "v9") is None
    assert R.row_for(None, "v3") is None

def test_env_may_only_confirm_the_pin(monkeypatch):
    monkeypatch.setenv("UNISWAP_V3_POSITION_MANAGER_ROBINHOOD",
                       "0x73991A25C818BF1F1128DEAAB1492D45638DE0D3")
    assert R.resolve_position_manager("robinhood", "v3").lower() == \
        "0x73991a25c818bf1f1128deaab1492d45638de0d3"
    monkeypatch.setenv("UNISWAP_V3_POSITION_MANAGER_ROBINHOOD",
                       "0x000000000000000000000000000000000000dEaD")
    with pytest.raises(ValueError, match="only CONFIRM"):
        R.resolve_position_manager("robinhood", "v3")

def test_position_managers_covers_every_protocol_on_a_chain():
    pm = R.position_managers("base")
    assert "0x03a520b32c04bf3beef7beb72e919cf822ed34f1" in pm
    assert "0x7c5f5a4bbd8fd63184577525326123b519429bdc" in pm
    assert R.position_managers("optimism") == frozenset()

def test_every_pinned_write_target_has_a_code_hash():
    for row in R.all_rows():
        for addr in (row.factory, row.position_manager, row.pool_manager):
            if addr:
                assert (row.chain, addr.lower()) in R.CODE_HASHES, (row.chain, addr)

def test_verify_pins_refuses_a_hash_mismatch():
    def rpc(method, params):
        assert method == "eth_getCode"
        return "0x6001600155"
    with pytest.raises(R.DexPinError, match="hashes to"):
        R.verify_pins(rpc, "robinhood", "v3")

def test_verify_pins_refuses_missing_code():
    with pytest.raises(R.DexPinError, match="NO code"):
        R.verify_pins(lambda m, p: "0x", "robinhood", "v3")


# Order 0013: arbitrum + polygon, hashes measured via eth_getCode on two RPCs and
# cross-linked on-chain (NPM.factory, PosM/StateView/Quoter.poolManager).
_FEED = {
    "arbitrum": (42161, "0x360e68faccca8ca495c1b759fd9eee466db9fb32",
                 "0xd88f38f930b7952f2db2432cb002e7abbf3dd869",
                 "0x76fd297e2d437cd7f76d50f01afe6160f86e9990"),
    "polygon": (137, "0x67366782805870060151383f4bbff9dab53e5cd6",
                "0x1ec2ebf4f37e7363fdfe3551602425af0b3ceef9",
                "0x5ea1bd7974c8a611cbab0bdcafcb1d9cc9b3ba5a"),
}


@pytest.mark.parametrize("chain", sorted(_FEED))
def test_arbitrum_polygon_rows_match_the_feed(chain):
    cid, pm, posm, sv = _FEED[chain]
    v3, v4 = R.row_for(chain, "v3"), R.row_for(chain, "v4")
    assert v3.chain_id == v4.chain_id == cid
    assert v3.factory.lower() == "0x1f98431c8ad98523631ae4a59f267346ea31f984"
    assert v3.position_manager.lower() == "0xc36442b4a4522e871399cd717abdd847ab11fe88"
    assert (v4.pool_manager.lower(), v4.position_manager.lower(),
            v4.state_view.lower()) == (pm, posm, sv)
    assert v4.permit2.lower() == R._PERMIT2.lower()


@pytest.mark.parametrize("chain", sorted(_FEED))
def test_arbitrum_polygon_every_verified_target_is_hashed(chain):
    for proto in ("v3", "v4"):
        row = R.row_for(chain, proto)
        for addr in (row.factory, row.position_manager, row.pool_manager,
                     *((row.state_view, row.permit2) if proto == "v4" else ())):
            if addr:
                assert (chain, addr.lower()) in R.CODE_HASHES, (chain, addr)


@pytest.mark.parametrize("chain", sorted(_FEED))
def test_verify_pins_refuses_changed_code_on_new_chains(chain):
    def rpc(method, params):
        return "0x6001"  # code exists, but is not what was measured
    for proto in ("v3", "v4"):
        with pytest.raises(R.DexPinError, match="hashes to"):
            R.verify_pins(rpc, chain, proto)
