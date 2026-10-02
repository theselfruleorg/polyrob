"""071 W2 — Solana mint facts and EVM contract facts read from the chain.

Payloads are trimmed copies of LIVE answers recorded 2026-10-02 (PYUSD
Token-2022 mint, a pump.fun curve on and off the curve, USDC proxy slots on
Base). No test reaches the network: every reader takes an injected ``rpc``.
"""
import base64
import struct

import pytest

from core.wallet import evm_facts, spl_facts
from core.wallet.solana_onchain import SPL_TOKEN_PROGRAM, TOKEN_2022_PROGRAM

PYUSD = "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo"
AUTH = "2apBGMsS6ti9RyF5TwQTDswXBWskiJP2LD4cUEDqYJjk"
PUMP_MINT = "Hu6xf6bNiJ5ErRaG7Yhos89jgfAdCG1tu8DCips2pump"


def _mint_value(owner=TOKEN_2022_PROGRAM, extensions=None, mint_auth="8Jornc27vtAYPkwDzsZVgLQchAYyC8nD7aCNPCDV8Qk2",
                freeze_auth=AUTH, supply="752844712063254", decimals=6):
    info = {"decimals": decimals, "freezeAuthority": freeze_auth, "isInitialized": True,
            "mintAuthority": mint_auth, "supply": supply}
    if extensions is not None:
        info["extensions"] = extensions
    return {"owner": owner, "executable": False,
            "data": {"parsed": {"info": info, "type": "mint"},
                     "program": "spl-token-2022" if owner == TOKEN_2022_PROGRAM else "spl-token"}}


PYUSD_EXTS = [
    {"extension": "mintCloseAuthority", "state": {"closeAuthority": AUTH}},
    {"extension": "permanentDelegate", "state": {"delegate": AUTH}},
    {"extension": "transferFeeConfig", "state": {
        "newerTransferFee": {"epoch": 605, "maximumFee": 0, "transferFeeBasisPoints": 0},
        "olderTransferFee": {"epoch": 605, "maximumFee": 0, "transferFeeBasisPoints": 0},
        "transferFeeConfigAuthority": AUTH, "withdrawWithheldAuthority": AUTH, "withheldAmount": 0}},
    {"extension": "confidentialTransferMint", "state": {"authority": AUTH}},
    {"extension": "transferHook", "state": {"authority": AUTH, "programId": None}},
    {"extension": "metadataPointer", "state": {"authority": AUTH, "metadataAddress": PYUSD}},
    {"extension": "tokenMetadata", "state": {"name": "PayPal USD", "symbol": "PYUSD",
                                             "updateAuthority": AUTH}},
]


def test_parse_token2022_mint_reads_authorities_and_extensions():
    f = spl_facts.parse_mint(PYUSD, _mint_value(extensions=PYUSD_EXTS))
    assert f.token_2022 and f.decimals == 6
    assert f.mint_authority and f.freeze_authority == AUTH
    assert f.extensions["permanentDelegate"]["delegate"] == AUTH
    assert f.supply_human == pytest.approx(752844712.063254)
    assert f.unassessed_extensions == []


def test_revoked_authorities_are_none_not_missing():
    f = spl_facts.parse_mint("M", _mint_value(owner=SPL_TOKEN_PROGRAM, mint_auth=None, freeze_auth=None))
    assert f.mint_authority is None and f.freeze_authority is None
    assert not f.token_2022


def test_absent_authority_keys_are_unreadable_never_revoked():
    v = _mint_value()
    del v["data"]["parsed"]["info"]["mintAuthority"]
    with pytest.raises(ValueError):
        spl_facts.parse_mint("M", v)


def test_not_a_mint_is_named():
    with pytest.raises(spl_facts.NotAMint):
        spl_facts.parse_mint("M", None)
    with pytest.raises(spl_facts.NotAMint):
        spl_facts.parse_mint("M", {"owner": "11111111111111111111111111111111", "data": ["", "base64"]})


def test_unknown_extension_is_listed_as_unassessed():
    f = spl_facts.parse_mint("M", _mint_value(extensions=[{"extension": "brandNewThing", "state": {}}]))
    assert f.unassessed_extensions == ["brandNewThing"]


def _curve_blob(vt, vs, rt, rs, total, complete, creator=b"\x01" * 32):
    return base64.b64encode(spl_facts.PUMP_CURVE_DISCRIMINATOR
                            + struct.pack("<5QB", vt, vs, rt, rs, total, complete)
                            + creator).decode()


def test_pump_curve_on_curve_decodes_reserves_and_progress():
    v = {"owner": spl_facts.PUMP_FUN_PROGRAM,
         "data": [_curve_blob(1073000000000000, 30000000001, 793100000000000 // 2, 40_000_000_000,
                              10 ** 15, 0), "base64"]}
    c = spl_facts.parse_pump_curve("CURVE", v)
    assert c.state == "on_curve"
    assert c.sold_fraction == pytest.approx(0.5)
    assert c.sol_in_curve == pytest.approx(40.0)


def test_pump_curve_graduated_and_not_pump():
    v = {"owner": spl_facts.PUMP_FUN_PROGRAM, "data": [_curve_blob(0, 0, 0, 0, 10 ** 15, 1), "base64"]}
    assert spl_facts.parse_pump_curve("C", v).state == "graduated"
    assert spl_facts.parse_pump_curve("C", None).state == "not_pump"
    # The USDC curve PDA exists as an EMPTY System account: not a curve.
    assert spl_facts.parse_pump_curve("C", {"owner": "11111111111111111111111111111111",
                                            "data": ["", "base64"]}).state == "not_pump"


def test_pump_curve_wrong_discriminator_is_an_error_not_a_state():
    blob = base64.b64encode(b"\x00" * 8 + b"\x00" * 41).decode()
    with pytest.raises(ValueError):
        spl_facts.parse_pump_curve("C", {"owner": spl_facts.PUMP_FUN_PROGRAM, "data": [blob, "base64"]})


def test_pump_curve_pda_matches_the_live_chain():
    pytest.importorskip("solders")
    # Derived and read live 2026-10-02.
    assert spl_facts.pump_curve_address(PUMP_MINT) == "41ka546oDhvZ1YxgGmQgr8C874vE9szhnAWNRH34f73y"


def test_holder_shares_resolve_owner_and_program_owned():
    pytest.importorskip("solders")
    curve = "41ka546oDhvZ1YxgGmQgr8C874vE9szhnAWNRH34f73y"   # a PDA: off-curve
    wallet = AUTH                                             # a normal key: on-curve
    largest = {"value": [{"address": "TA1", "amount": "600"}, {"address": "TA2", "amount": "300"},
                         {"address": "TA3", "amount": "0"}]}
    owners = {"value": [{"data": {"parsed": {"info": {"owner": curve}}}},
                        {"data": {"parsed": {"info": {"owner": wallet}}}}, None]}
    rows = spl_facts.holder_shares(largest, owners, 1000, labels={curve: "pump.fun bonding curve"})
    assert [r.share for r in rows] == [0.6, 0.3], "zero rows dropped"
    assert rows[0].program_owned is True and rows[0].label == "pump.fun bonding curve"
    assert rows[1].program_owned is False
    top1, top10, people = spl_facts.concentration(rows)
    assert (top1, top10, people) == (0.6, pytest.approx(0.9), 0.3)


def test_unknown_supply_leaves_shares_unknown():
    rows = spl_facts.holder_shares({"value": [{"address": "TA", "amount": "5"}]}, None, None)
    assert rows[0].share is None and rows[0].owner is None
    assert spl_facts.concentration(rows) == (None, None, None)


def test_read_holders_survives_failed_owner_resolution():
    def rpc(method, params, timeout=8.0):
        if method == "getTokenLargestAccounts":
            return {"value": [{"address": "TA", "amount": "10"}]}
        raise RuntimeError("429")
    rows = spl_facts.read_holders("M", 100, rpc=rpc)
    assert rows[0].share == 0.1 and rows[0].owner is None


# --------------------------------------------------------------------------
# EVM
# --------------------------------------------------------------------------

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
W = lambda a: "0x" + "0" * 24 + a[2:].lower()  # noqa: E731
ZERO_WORD = "0x" + "0" * 64


def _evm_rpc(slots, owner_ret="revert", code="0x6080", admin_code="0x", admin_owner=None):
    def rpc(method, params, timeout=6.0):
        if method == "eth_getCode":
            return code if params[0] == USDC_BASE else admin_code
        if method == "eth_getStorageAt":
            return slots.get(params[1], ZERO_WORD)
        if method == "eth_call":
            target = params[0]["to"]
            ret = owner_ret if target == USDC_BASE else admin_owner
            if ret == "revert":
                raise RuntimeError("execution reverted")
            return ret
        raise AssertionError(method)
    return rpc


def test_usdc_legacy_zos_proxy_is_seen():
    slots = {evm_facts.PROXY_SLOTS["zos_implementation"]: W("0x2ce6311ddae708829bc0784c967b7d77d19fd779"),
             evm_facts.PROXY_SLOTS["zos_admin"]: W("0x4fc7850364958d97b4d3f5a08f79db2493f8ca44")}
    f = evm_facts.read_contract("base", USDC_BASE,
                                rpc=_evm_rpc(slots, owner_ret=W("0x3abd6f64a422225e61e435bae41db12096106df7")))
    assert f.proxy_kind == "zos_legacy"
    assert f.implementation == "0x2ce6311ddae708829bc0784c967b7d77d19fd779"
    assert f.admin == "0x4fc7850364958d97b4d3f5a08f79db2493f8ca44"
    assert f.owner_state == "set"


def test_eip1967_proxy_admin_owner_is_followed_one_hop():
    admin = "0x" + "ab" * 20
    slots = {evm_facts.PROXY_SLOTS["eip1967_implementation"]: W("0x" + "11" * 20),
             evm_facts.PROXY_SLOTS["eip1967_admin"]: W(admin)}
    f = evm_facts.read_contract("base", USDC_BASE, rpc=_evm_rpc(
        slots, admin_code="0x6080", admin_owner=W("0x" + "cd" * 20)))
    assert f.proxy_kind == "eip1967" and f.admin_owner == "0x" + "cd" * 20
    assert f.owner_state == "no_owner_function"


def test_renounced_owner_and_no_proxy():
    f = evm_facts.read_contract("base", USDC_BASE, rpc=_evm_rpc({}, owner_ret=ZERO_WORD))
    assert f.owner_state == "renounced" and f.proxy_kind == "none_found"


def test_no_code_is_not_a_contract():
    f = evm_facts.read_contract("base", USDC_BASE, rpc=_evm_rpc({}, code="0x"))
    assert f.has_code is False and f.proxy_kind is None


def test_a_failed_slot_read_means_proxy_unknown_not_none_found():
    def rpc(method, params, timeout=6.0):
        if method == "eth_getStorageAt" and params[1] == evm_facts.PROXY_SLOTS["zos_admin"]:
            raise RuntimeError("timeout")
        return _evm_rpc({}, owner_ret=ZERO_WORD)(method, params)
    f = evm_facts.read_contract("base", USDC_BASE, rpc=rpc)
    assert f.proxy_kind is None, "an unread slot must not let us say 'no proxy'"


def test_every_read_failing_raises():
    def rpc(*a, **k):
        raise RuntimeError("down")
    with pytest.raises(RuntimeError):
        evm_facts.read_contract("base", USDC_BASE, rpc=rpc)
