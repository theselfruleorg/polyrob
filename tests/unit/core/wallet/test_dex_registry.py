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
    assert R.row_for("polygon", "v3") is None
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
    assert R.position_managers("polygon") == frozenset()

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
