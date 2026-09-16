"""Holder concentration is the free half of the answer GoPlus already sends.

`token_info` parsed the boolean risks, the two taxes and `is_open_source`, and
threw away `holders`, `holder_count`, `lp_holders`, `lp_total_supply`,
`creator_percent`, `owner_percent` and `honeypot_with_same_creator`. That is the
"who owns this and can they pull the floor" answer — the thing the owner calls a
bubble map — arriving free in a call the tool already makes and then discards.

Also pinned here: a screen that could only run SOME of its checks must say so.
GoPlus covers Robinhood chain 4663, but answers it thinly (no is_honeypot, no
holders, empty taxes). Rendering that as "no risk flags raised" makes a partial
screen read exactly like a clean one.
"""
import pytest

from tools.defi.providers import goplus


def _evm(**over):
    data = {
        "is_honeypot": "0", "cannot_sell_all": "0", "cannot_buy": "0",
        "is_blacklisted": "0", "is_proxy": "0", "is_mintable": "0",
        "transfer_pausable": "0", "hidden_owner": "0",
        "can_take_back_ownership": "0", "selfdestruct": "0",
        "slippage_modifiable": "0", "personal_slippage_modifiable": "0",
        "trading_cooldown": "0",
        "buy_tax": "0", "sell_tax": "0", "is_open_source": "1",
        "holder_count": "35236", "total_supply": "1000000000",
        "holders": [
            {"address": "0xaaa", "tag": "", "is_contract": 1,
             "balance": "300000000", "percent": "0.300000000000000000", "is_locked": 0},
            {"address": "0xbbb", "tag": "Uniswap V3", "is_contract": 1,
             "balance": "100000000", "percent": "0.100000000000000000", "is_locked": 1},
            {"address": "0xccc", "tag": "", "is_contract": 0,
             "balance": "50000000", "percent": "0.050000000000000000", "is_locked": 0},
        ],
        "lp_holder_count": "4", "lp_total_supply": "26.189228718361743",
        "lp_holders": [
            {"address": "0xddd", "tag": "", "is_contract": 0,
             "balance": "26.18", "percent": "0.999928862347480456", "is_locked": 0},
        ],
        "creator_address": "0xdead", "creator_percent": "0.000000",
        "owner_address": "0xbeef", "owner_percent": "0.0",
        "honeypot_with_same_creator": "0",
    }
    data.update(over)
    return {"code": 1, "result": {"0xtoken": data}}


# --- N1: the holder report ------------------------------------------------

def test_holders_are_parsed_with_percent_and_contract_flag():
    r = goplus.parse_holders(_evm())
    assert r.available is True
    assert [h.address for h in r.top_holders] == ["0xaaa", "0xbbb", "0xccc"]
    assert r.top_holders[0].percent == pytest.approx(0.30)
    assert r.top_holders[0].is_contract is True
    assert r.top_holders[1].is_locked is True
    assert r.top_holders[2].is_contract is False


def test_holder_count_and_supply_are_carried():
    r = goplus.parse_holders(_evm())
    assert r.holder_count == 35236
    assert r.total_supply == pytest.approx(1_000_000_000.0)


def test_top_holder_concentration_is_summed():
    r = goplus.parse_holders(_evm())
    assert r.top_percent == pytest.approx(0.45)


def test_lp_holders_and_lock_state_are_carried():
    r = goplus.parse_holders(_evm())
    assert r.lp_holder_count == 4
    assert r.lp_total_supply == pytest.approx(26.189228718361743)
    assert r.lp_holders[0].percent == pytest.approx(0.999928862347480456)


def test_creator_and_owner_stake_are_carried():
    r = goplus.parse_holders(_evm())
    assert r.creator_address == "0xdead"
    assert r.creator_percent == pytest.approx(0.0)
    assert r.owner_address == "0xbeef"
    assert r.honeypot_with_same_creator is False


def test_a_serial_scammer_creator_is_flagged():
    r = goplus.parse_holders(_evm(honeypot_with_same_creator="1"))
    assert r.honeypot_with_same_creator is True


def test_a_payload_without_holder_fields_is_unavailable_not_empty():
    """The Robinhood-chain shape: the call answers, with none of this in it.

    `available=False` with a reason, never an empty list that reads as
    'nobody holds this token'.
    """
    thin = {"code": 1, "result": {"0xtoken": {
        "buy_tax": "", "sell_tax": "", "cannot_buy": "0",
        "is_open_source": "0", "is_in_dex": "0",
        "creator_address": "0xdead", "creator_percent": "0.000000",
        "honeypot_with_same_creator": "0"}}}
    r = goplus.parse_holders(thin)
    assert r.available is False
    assert r.reason and "holder" in r.reason.lower()
    assert r.top_holders == []


def test_an_error_payload_is_unavailable():
    assert goplus.parse_holders({"code": 0}).available is False
    assert goplus.parse_holders(None).available is False


# --- D6: a partial screen must not read as a clean screen -----------------

def test_a_full_evm_screen_reports_nothing_missing():
    v = goplus.parse_screen(_evm())
    assert v.available is True
    assert v.missing == []


def test_a_thin_chain_payload_names_the_checks_that_did_not_run():
    thin = {"code": 1, "result": {"0xtoken": {
        "buy_tax": "", "sell_tax": "", "cannot_buy": "0",
        "is_open_source": "0", "is_in_dex": "0"}}}
    v = goplus.parse_screen(thin)
    assert v.available is True
    assert "is_honeypot" in v.missing
    assert "is_mintable" in v.missing
    assert "cannot_buy" not in v.missing


def test_an_empty_tax_string_counts_as_not_checked():
    """GoPlus sends `buy_tax: ""` when it did not compute one. Recording that
    as a check that ran reads as "the tax is fine"."""
    v = goplus.parse_screen(_evm(buy_tax="", sell_tax=""))
    assert "buy_tax" in v.missing and "sell_tax" in v.missing
    assert "buy_tax" not in v.checks


def test_solana_screen_also_reports_missing_checks():
    sol = {"code": 1, "result": {"Mint": {"mintable": "0", "freezable": "0"}}}
    v = goplus.parse_solana_screen(sol)
    assert v.available is True
    assert "closable" in v.missing
