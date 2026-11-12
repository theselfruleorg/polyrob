"""``EvmRail.size_gas`` when MAX_GAS_LIMIT, not the x1.5 margin, bounds the limit (115 R1).

The capped limit must keep x1.2 of the measured need; below that the rail refuses and names
the remedy (split the call). The figures are the rig's measurements (tests/integration/
test_agent_nft_rig.py) and the live ones already pinned in test_broadcast_evm.py.
"""
import pytest

from core.wallet.broadcast import evm as rail
from core.wallet.signer import LocalEoaSigner

KEY = bytes.fromhex("59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d")
#: 0.024 gwei (Robinhood-measured): the FEE ceiling never binds here, so the GAS rule is measured.
FEE = 24_000_000
ACCOUNT_EXECUTE = "0x51945447"


def _rail():
    return rail.EvmRail(chain="base", signer=LocalEoaSigner(KEY))


def _tx(data="0x"):
    return {"maxFeePerGas": FEE, "gas": rail.DEFAULT_GAS_LIMIT, "data": data, "to": "0x" + "11" * 20,
            "value": 0}


@pytest.mark.parametrize("what, used", [
    ("a 1-id collection mint (rig)", 865_437),
    ("an aggregator swap route (live, Base)", 1_110_000),
    ("reveal(64) (rig)", 2_238_231),
    ("a Pons V2 launch (live)", 3_468_850),
    ("the last uncapped measurement", rail.MAX_GAS_LIMIT * 2 // 3),
])
def test_an_uncapped_transaction_keeps_the_x1_5_margin(what, used):
    assert used * 3 // 2 <= rail.MAX_GAS_LIMIT, what
    assert _rail().size_gas(_tx(), used)["gas"] == used * 3 // 2, what


def test_an_8_id_mint_is_capped_and_still_keeps_more_than_x1_2():
    used = 5_930_660                                  # rig: an 8-id mint
    sized = _rail().size_gas(_tx(), used)["gas"]
    assert sized == rail.MAX_GAS_LIMIT and sized / used > 1.2


def test_the_threshold_is_max_gas_limit_over_1_2():
    r = _rail()
    assert r.size_gas(_tx(), 6_666_667)["gas"] == rail.MAX_GAS_LIMIT
    with pytest.raises(rail.GasCeilingExceeded):
        r.size_gas(_tx(), 6_666_668)


def test_the_measured_10_id_mint_is_refused_with_the_remedy():
    """Rig: simulated 7,421,192, landed at 7,487,948 under an 8M limit (6.8%); eth_estimateGas
    said 7,714,733 — the collection needs IDENTITY_GAS x 64/63 + 10,000 LEFT per identity."""
    with pytest.raises(rail.GasCeilingExceeded) as exc:
        _rail().size_gas(_tx(), 7_421_192)
    text = str(exc.value)
    assert "Split the call" in text and "fewer ids per mint" in text
    assert "7421192" in text and "578808" in text and "refusing to sign" in text


def test_the_absurd_simulation_brake_is_unchanged():
    with pytest.raises(rail.GasCeilingExceeded, match="above the 8000000 gas-limit ceiling"):
        _rail().size_gas(_tx(), rail.MAX_GAS_LIMIT + 1)


def test_an_account_call_is_held_to_its_estimate_too(monkeypatch):
    """An ERC-6551 account call's need is eth_estimateGas: a 7M estimate fits under the cap
    (it passed before) but leaves under x1.2, so it refuses; a 5M estimate gets 6M."""
    r = _rail()
    monkeypatch.setattr(r, "estimate_gas", lambda tx: 7_000_000)
    with pytest.raises(rail.GasCeilingExceeded, match="Split the call"):
        r.size_gas(_tx(ACCOUNT_EXECUTE + "00" * 32), 3_000_000)
    monkeypatch.setattr(r, "estimate_gas", lambda tx: 5_000_000)
    assert r.size_gas(_tx(ACCOUNT_EXECUTE + "00" * 32), 3_000_000)["gas"] == 6_000_000


def test_an_ordinary_account_call_is_unchanged(monkeypatch):
    """Measured on a 4663 fork: the W8 batch uses 179,980 and needs 403,473."""
    r = _rail()
    monkeypatch.setattr(r, "estimate_gas", lambda tx: 403_473)
    assert r.size_gas(_tx(ACCOUNT_EXECUTE + "00" * 32), 179_980)["gas"] == 403_473 * 6 // 5
